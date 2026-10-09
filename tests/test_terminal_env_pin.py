"""Tests for deploy/shared/terminal_env_pin.py.

    python3 -m unittest discover -s tests -p 'test_terminal_env_pin.py'

The file being rewritten is a profile's .env: it holds the profile's secrets, and the
entrypoint rewrites every one of them on every start. So what matters is that the rewrite
changes the managed TERMINAL_* lines and nothing else, keeps the file's owner and mode
(two containers with different users share it through one group), and never leaves a
half-written file.

HermesTest asks a real Hermes what the scope resolves, and is skipped where Hermes is not
importable, as in CI; the image build runs `terminal_env_pin.py --build-check` against the
Hermes it ships. Locally: PYTHONPATH=<hermes-agent checkout> before the command above.
"""

import importlib.util
import io
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SHARED = _ROOT / "deploy" / "shared"
_OPERATOR_GOLDEN = _ROOT / "k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml"
_spec = importlib.util.spec_from_file_location("terminal_env_pin", _SHARED / "terminal_env_pin.py")
tep = importlib.util.module_from_spec(_spec)
sys.modules["terminal_env_pin"] = tep
_spec.loader.exec_module(tep)

try:
    from hermes_cli import managed_scope
    from hermes_constants import mark_named_profile_deleted
    from tools import terminal_scope  # noqa: F401

    HAS_HERMES = True
except ImportError:
    HAS_HERMES = False

PINNED = {
    "TERMINAL_ENV": "ssh",
    "TERMINAL_SSH_HOST": "platform-agent-shell-0.platform-agent-shell.kubeagents-system.svc.cluster.local",
    "TERMINAL_SSH_PORT": "2222",
}


# PyYAML parses this as a date and raises ValueError, not YAMLError.
BAD_DATE = "updated: 2026-13-45\n"


def pinned_lines():
    return "".join(f"{key}={value}\n" for key, value in PINNED.items())


class EnvRewriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = self.tmp / ".env"

    def write(self, data, mode=0o660):
        self.env.write_bytes(data if isinstance(data, bytes) else data.encode())
        os.chmod(self.env, mode)

    def rewrite(self):
        return tep.write_env(self.env, PINNED)

    def leftovers(self):
        return sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(tep.TEMP_PREFIX))

    def test_other_lines_survive_byte_for_byte(self):
        others = (
            "# written by the installer\n"
            "\n"
            "API_SERVER_KEY=abc123\n"
            'SLACK_BOT_TOKEN="xoxb with spaces" # inline comment\n'
            "export KUBECONFIG=/opt/data/kube/config\n"
            "  INDENTED = value\n"
        )
        self.write(others)
        self.rewrite()
        self.assertEqual(others + pinned_lines(), self.env.read_text())

    def test_old_managed_lines_are_replaced_where_they_stood(self):
        self.write(
            "A=1\n"
            "export TERMINAL_ENV=local\n"
            "B=2\n"
            "TERMINAL_SSH_HOST = old-host\n"
            "TERMINAL_ENV=docker\n"
        )
        self.rewrite()
        self.assertEqual(
            "A=1\n"
            "TERMINAL_ENV=ssh\n"
            "B=2\n"
            f"TERMINAL_SSH_HOST={PINNED['TERMINAL_SSH_HOST']}\n"
            "TERMINAL_SSH_PORT=2222\n",
            self.env.read_text(),
        )

    def test_a_terminal_setting_the_managed_block_does_not_own_stays(self):
        # `hermes config set terminal.timeout 600` mirrors into .env like this.
        self.write("TERMINAL_TIMEOUT=600\nTERMINAL_ENV=local\n")
        self.rewrite()
        self.assertIn("TERMINAL_TIMEOUT=600\n", self.env.read_text())
        self.assertNotIn("TERMINAL_ENV=local", self.env.read_text())

    def test_commented_out_managed_lines_are_left_alone(self):
        self.write("# TERMINAL_ENV=local\n")
        self.rewrite()
        self.assertEqual("# TERMINAL_ENV=local\n" + pinned_lines(), self.env.read_text())

    def test_a_last_line_without_a_newline_is_not_joined_to_the_pin(self):
        self.write("API_SERVER_KEY=abc123")
        self.rewrite()
        self.assertEqual("API_SERVER_KEY=abc123\n" + pinned_lines(), self.env.read_text())

    def test_crlf_bom_and_non_utf8_bytes_survive(self):
        original = b"\xef\xbb\xbfA=1\r\nTERMINAL_ENV=local\r\nNOTE=caf\xe9\r\n"
        self.write(original)
        self.rewrite()
        self.assertEqual(
            b"\xef\xbb\xbfA=1\r\nTERMINAL_ENV=ssh\r\nNOTE=caf\xe9\r\n"
            + f"TERMINAL_SSH_HOST={PINNED['TERMINAL_SSH_HOST']}\nTERMINAL_SSH_PORT=2222\n".encode(),
            self.env.read_bytes(),
        )

    def test_the_mode_is_kept(self):
        self.write("A=1\n", mode=0o640)
        self.rewrite()
        self.assertEqual(0o640, stat.S_IMODE(self.env.stat().st_mode))

    def test_the_owner_and_group_are_kept(self):
        """A new file takes the writer's user and the directory's group; the rewrite must not."""
        primary = os.getegid()
        other = next((g for g in os.getgroups() if g != primary), None)
        if other is None:
            self.skipTest("the test user belongs to no second group")
        self.write("A=1\n")
        os.chown(self.env, -1, other)
        before = self.env.stat()
        self.rewrite()
        after = self.env.stat()
        self.assertEqual((before.st_uid, before.st_gid), (after.st_uid, after.st_gid))

    def test_a_file_owned_by_another_user_keeps_its_group(self):
        """Not root, the rewrite cannot give the file back to its owner; it keeps the group."""
        self.write("A=1\n", mode=0o640)
        real = self.env.stat()
        foreign = os.stat_result((real.st_mode, real.st_ino, real.st_dev, real.st_nlink, real.st_uid + 1, *real[5:10]))
        real_stat = pathlib.Path.stat
        calls = []

        def fake_stat(path, **kwargs):
            return foreign if path == self.env else real_stat(path, **kwargs)

        def fake_fchown(fd, uid, gid):
            calls.append((uid, gid))
            if uid != -1:
                raise PermissionError(1, "Operation not permitted")

        with mock.patch.object(pathlib.Path, "stat", fake_stat), mock.patch.object(tep.os, "fchown", fake_fchown):
            self.assertTrue(self.rewrite())
        self.assertEqual([(real.st_uid + 1, real.st_gid), (-1, real.st_gid)], calls)
        self.assertEqual("A=1\n" + pinned_lines(), self.env.read_text())
        self.assertEqual(0o640, stat.S_IMODE(self.env.stat().st_mode))

    def test_a_new_file_is_group_read_write(self):
        self.assertTrue(self.rewrite())
        self.assertEqual(pinned_lines(), self.env.read_text())
        self.assertEqual(tep.NEW_ENV_MODE, stat.S_IMODE(self.env.stat().st_mode))

    def test_an_already_pinned_file_is_not_rewritten(self):
        self.write("A=1\n" + pinned_lines())
        inode = self.env.stat().st_ino
        self.assertFalse(self.rewrite())
        self.assertEqual(inode, self.env.stat().st_ino)

    def test_the_replace_is_atomic(self):
        self.write("A=1\n")
        inode = self.env.stat().st_ino
        self.rewrite()
        self.assertNotEqual(inode, self.env.stat().st_ino)
        self.assertEqual([], self.leftovers())

    def test_a_failed_write_leaves_the_original_and_no_temp_file(self):
        self.write("A=1\nTERMINAL_ENV=local\n")
        with mock.patch.object(tep.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.rewrite()
        self.assertEqual("A=1\nTERMINAL_ENV=local\n", self.env.read_text())
        self.assertEqual([], self.leftovers())

    def test_a_symlinked_env_is_written_through(self):
        real = self.tmp / "real.env"
        real.write_text("A=1\n")
        self.env.symlink_to(real)
        self.rewrite()
        self.assertTrue(self.env.is_symlink())
        self.assertEqual("A=1\n" + pinned_lines(), real.read_text())


class SweepTest(unittest.TestCase):
    """Which profiles stop the pod. Hermes is stubbed at resolved_scope; HermesTest covers it."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.homes = [self.tmp / name for name in ("default", "platform", "cluster")]
        for home in self.homes:
            home.mkdir()
            (home / ".env").write_text("A=1\n")
        self.scopes = {home: dict(PINNED) for home in self.homes}
        for target, value in (
            ("managed_terminal_env", mock.Mock(return_value=dict(PINNED))),
            ("profile_homes", mock.Mock(return_value=self.homes)),
            ("resolved_scope", mock.Mock(side_effect=self.resolve)),
        ):
            patcher = mock.patch.object(tep, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def resolve(self, home):
        scope = self.scopes[home]
        if isinstance(scope, Exception):
            raise scope
        return scope

    def sweep(self):
        with mock.patch.object(tep.sys, "stderr", new_callable=io.StringIO) as err:
            code = tep.sweep(self.tmp)
        return code, err.getvalue()

    def test_every_profile_is_pinned(self):
        code, _ = self.sweep()
        self.assertEqual(0, code)
        for home in self.homes:
            self.assertEqual("A=1\n" + pinned_lines(), (home / ".env").read_text())

    def overridden(self, strip):
        platform = self.homes[1]
        self.scopes[platform] = {**PINNED, "TERMINAL_ENV": "local"}
        overrides = mock.patch.object(tep, "config_overrides", side_effect=lambda home, wrong: ["terminal.backend"] if wrong else [])
        with overrides, mock.patch.object(tep, "strip_config_keys", side_effect=strip) as stripped:
            code, err = self.sweep()
        return platform, stripped, code, err

    def test_a_config_key_that_outranks_the_copy_is_deleted_before_the_check(self):
        def strip(path, keys):
            self.scopes[path.parent] = dict(PINNED)

        platform, stripped, code, _ = self.overridden(strip)
        self.assertEqual(0, code)
        stripped.assert_called_once_with(platform / "config.yaml", ["terminal.backend"])

    def test_a_config_key_that_cannot_be_deleted_fails_the_sweep(self):
        _, _, code, err = self.overridden(tep.PinError("cannot delete terminal.backend"))
        self.assertEqual(1, code)
        self.assertIn("cannot delete terminal.backend", err)

    def test_a_profile_that_resolves_another_backend_fails_the_sweep_by_name(self):
        platform = self.homes[1]
        self.scopes[platform] = {**PINNED, "TERMINAL_ENV": "local"}
        code, err = self.sweep()
        self.assertEqual(1, code)
        last = err.strip().splitlines()[-1]
        self.assertIn(str(platform), last)
        self.assertNotIn(str(self.homes[2]), last)
        self.assertIn("Hermes resolves another TERMINAL_ENV", err)
        self.assertNotIn("local", err.replace(str(self.tmp), ""))
        self.assertEqual("A=1\n" + pinned_lines(), (self.homes[2] / ".env").read_text())

    def test_a_profile_hermes_cannot_read_is_reported_not_fatal(self):
        platform = self.homes[1]
        self.scopes[platform] = tep.ScopeUnavailable("cannot parse config.yaml")
        code, err = self.sweep()
        self.assertEqual(0, code)
        self.assertIn("WARN", err)
        self.assertIn("cannot parse config.yaml", err)

    def read_only(self, home):
        home.chmod(0o555)
        self.addCleanup(home.chmod, 0o755)

    def test_an_env_that_cannot_take_the_copy_fails_the_sweep_even_if_hermes_cannot_read_it(self):
        """Skipping it would leave nothing pinned for when the file is fixed in place."""
        platform = self.homes[1]
        (platform / ".env").unlink()
        (platform / ".env").mkdir()
        self.scopes[platform] = tep.ScopeUnavailable("cannot read .env")
        code, err = self.sweep()
        self.assertEqual(1, code)
        self.assertIn("cannot rewrite", err)

    @unittest.skipIf(os.geteuid() == 0, "root ignores the mode bits this test relies on")
    def test_an_env_that_cannot_take_the_copy_fails_the_sweep_even_if_hermes_resolves_the_values(self):
        """The values could come from config.yaml, which Hermes' next save strips."""
        platform = self.homes[1]
        self.read_only(platform)
        code, err = self.sweep()
        self.assertEqual(1, code)
        self.assertIn("cannot rewrite", err)
        self.assertEqual("A=1\n", (platform / ".env").read_text())

    def test_nothing_to_pin_leaves_every_file_alone(self):
        tep.managed_terminal_env.return_value = {}
        code, _ = self.sweep()
        self.assertEqual(0, code)
        for home in self.homes:
            self.assertEqual("A=1\n", (home / ".env").read_text())

    def test_a_managed_config_that_cannot_be_used_fails_before_any_write(self):
        tep.managed_terminal_env.side_effect = tep.PinError("no terminal block")
        code, err = self.sweep()
        self.assertEqual(1, code)
        self.assertIn("no terminal block", err)
        self.assertEqual("A=1\n", (self.homes[0] / ".env").read_text())


class BuildCheckTerminalTest(unittest.TestCase):
    """--build-check tests the terminal block the operator renders, not a stale copy of it."""

    def rendered_terminal(self):
        for doc in tep.yaml.safe_load_all(_OPERATOR_GOLDEN.read_text()):
            config = ((doc or {}).get("data") or {}).get("managed-config.yaml")
            if config:
                return tep.yaml.safe_load(config)["terminal"]
        self.fail(f"no managed-config.yaml in {_OPERATOR_GOLDEN}")

    def test_it_matches_the_operator_golden_but_for_the_host(self):
        """The golden's CR is named platformagent, so only its shell Service name differs."""
        rendered = self.rendered_terminal()
        self.assertEqual(sorted(tep.BUILD_CHECK_TERMINAL), sorted(rendered))
        for key, value in rendered.items():
            with self.subTest(key=key):
                if key == "ssh_host":
                    self.assertEqual(tep.BUILD_CHECK_TERMINAL[key].split(".", 2)[2], value.split(".", 2)[2])
                else:
                    self.assertEqual(tep.BUILD_CHECK_TERMINAL[key], value)


class ManagedBlockTest(unittest.TestCase):
    """When there is something to pin, in and out of an operator pod. No Hermes needed."""

    def setUp(self):
        self.managed = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.managed, ignore_errors=True)
        self.override = str(self.managed)

    def config(self, text):
        (self.managed / "config.yaml").write_text(text)

    def block(self, override):
        with mock.patch.object(tep.sys, "stderr", new_callable=io.StringIO):
            return tep.managed_terminal_block(self.managed, override)

    def test_outside_the_operator_nothing_to_pin_is_not_an_error(self):
        """/etc/hermes exists in every image, with no config.yaml unless one is mounted."""
        self.assertIsNone(tep.managed_terminal_block(None, ""))
        self.assertIsNone(self.block(""))
        for text in ("model:\n  default: x\n", "terminal: [unclosed\n", "terminal: local\n", BAD_DATE):
            with self.subTest(text=text):
                self.config(text)
                self.assertIsNone(self.block(""))

    def test_outside_the_operator_any_terminal_block_is_returned(self):
        self.config("terminal:\n  backend: docker\n")
        self.assertEqual({"backend": "docker"}, self.block(""))

    def test_in_an_operator_pod_anything_but_an_ssh_block_is_an_error(self):
        with self.assertRaises(tep.PinError):
            tep.managed_terminal_block(None, "/etc/hermes")
        with self.assertRaises(tep.PinError):
            self.block(self.override)
        for text in ("model:\n  default: x\n", "terminal: [unclosed\n", "terminal:\n  backend: local\n", BAD_DATE):
            with self.subTest(text=text):
                self.config(text)
                with self.assertRaises(tep.PinError):
                    self.block(self.override)

    def test_in_an_operator_pod_the_ssh_block_is_returned(self):
        self.config(tep.yaml.safe_dump({"terminal": tep.BUILD_CHECK_TERMINAL}))
        self.assertEqual(tep.BUILD_CHECK_TERMINAL, self.block(self.override))


class DescribeTest(unittest.TestCase):
    """A read error is logged by type and position, never by the text it quotes."""

    MARKER = "sk-not-for-the-log"

    def parse_error(self):
        try:
            tep.yaml.safe_load(f"key: [unclosed {self.MARKER}\n")
        except tep.yaml.YAMLError as exc:
            return exc
        self.fail("the YAML parsed")

    def test_a_yaml_error_keeps_its_position_and_drops_the_quoted_line(self):
        exc = self.parse_error()
        self.assertIn(self.MARKER, str(exc))
        self.assertRegex(tep.describe(exc), r"^\w+Error at line \d+, column \d+$")
        self.assertNotIn(self.MARKER, tep.describe(exc))

    def test_a_wrapped_yaml_error_is_described_by_its_cause(self):
        cause = self.parse_error()
        try:
            raise RuntimeError(f"cannot parse config.yaml: {cause}") from cause
        except RuntimeError as exc:
            self.assertEqual(tep.describe(cause), tep.describe(exc))

    def test_an_os_error_keeps_its_path(self):
        exc = FileNotFoundError(2, "No such file or directory", "/opt/data/config.yaml")
        self.assertIn("/opt/data/config.yaml", tep.describe(exc))

    def test_any_other_error_is_its_type(self):
        self.assertEqual("ValueError", tep.describe(ValueError(self.MARKER)))

    def test_a_profile_hermes_cannot_parse_is_reported_without_the_line(self):
        cause = self.parse_error()
        unavailable = type("TerminalPolicyUnavailable", (Exception,), {})
        scope = types.ModuleType("tools.terminal_scope")
        scope.TerminalPolicyUnavailable = unavailable

        def build(home):
            raise unavailable(f"cannot parse {home}/config.yaml: {cause}") from cause

        scope.build_profile_terminal_scope = build
        modules = {"tools": types.ModuleType("tools"), "tools.terminal_scope": scope}
        with mock.patch.dict(sys.modules, modules), self.assertRaises(tep.ScopeUnavailable) as caught:
            tep.resolved_scope(pathlib.Path("/opt/data"))
        self.assertNotIn(self.MARKER, str(caught.exception))
        self.assertIn("line", str(caught.exception))


class ManagedEnvTest(unittest.TestCase):
    """A value .env cannot hold. Hermes' mapping is faked so CI runs this."""

    def setUp(self):
        self.managed = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.managed, ignore_errors=True)
        terminal = {**tep.BUILD_CHECK_TERMINAL, "docker_volumes": ["/a:/b"]}
        (self.managed / "config.yaml").write_text(tep.yaml.safe_dump({"terminal": terminal}))
        config = types.ModuleType("hermes_cli.config")
        config.TERMINAL_CONFIG_ENV_MAP = {"backend": "TERMINAL_ENV", "docker_volumes": "TERMINAL_DOCKER_VOLUMES"}
        config._terminal_config_value_is_bridgeable = lambda key, value: True
        config._terminal_env_value = lambda value: json.dumps(value) if isinstance(value, list) else str(value)
        scope = types.ModuleType("hermes_cli.managed_scope")
        scope.get_managed_dir = lambda: self.managed
        modules = {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.config": config}
        modules["hermes_cli.managed_scope"] = scope
        env = {k: v for k, v in os.environ.items() if k != tep.MANAGED_DIR_ENV}
        for patcher in (mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, env, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_outside_the_operator_it_is_left_out(self):
        with mock.patch.object(tep.sys, "stderr", new_callable=io.StringIO) as err:
            self.assertEqual({"TERMINAL_ENV": "ssh"}, tep.managed_terminal_env())
        self.assertIn("terminal.docker_volumes", err.getvalue())

    def test_in_an_operator_pod_it_is_an_error(self):
        os.environ[tep.MANAGED_DIR_ENV] = str(self.managed)
        with self.assertRaises(tep.PinError):
            tep.managed_terminal_env()

    def test_a_backend_hermes_does_not_map_is_an_error_in_or_out_of_the_operator(self):
        (self.managed / "config.yaml").write_text(tep.yaml.safe_dump({"terminal": tep.BUILD_CHECK_TERMINAL}))
        del sys.modules["hermes_cli.config"].TERMINAL_CONFIG_ENV_MAP["backend"]
        for override in ("", str(self.managed)):
            with self.subTest(override=override):
                os.environ[tep.MANAGED_DIR_ENV] = override
                with self.assertRaisesRegex(tep.PinError, "terminal.backend"):
                    tep.managed_terminal_env()


class TerminalDefaultMirrorTest(unittest.TestCase):
    """The preflight's mirror of the terminal tool's default, against a Hermes tree
    written here, so CI runs the parse and the mismatch without Hermes importable.
    The image build runs the same check against the Hermes it ships."""

    TOOL_LINE = '        "timeout": _parse_env_var("TERMINAL_TIMEOUT", "{seconds}"),\n'

    def hermes_tree(self, seconds, line=None):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tool = root / tep.TERMINAL_TOOL_FILE
        tool.parent.mkdir(parents=True)
        tool.write_text("FOREGROUND_MAX_TIMEOUT = 600\n" + (self.TOOL_LINE.format(seconds=seconds) if line is None else line))
        return root

    def test_the_mirror_is_read_from_the_preflight_in_the_checkout(self):
        script = pathlib.Path(__file__).resolve().parents[1] / "agents" / "platform" / "scripts" / tep.PREFLIGHT_SCRIPT_NAME
        declared = tep.PREFLIGHT_TERMINAL_MIRROR_RE.search(script.read_text())
        self.assertIsNotNone(declared, "cluster_preflight.sh no longer declares TERMINAL_TOOL_TIMEOUT_SECONDS")
        self.assertEqual(int(declared.group(1)), tep.preflight_terminal_mirror_seconds())

    def test_the_default_is_read_from_the_terminal_tool_source(self):
        mirror = tep.preflight_terminal_mirror_seconds()
        self.assertEqual(mirror, tep.hermes_terminal_default_seconds(self.hermes_tree(mirror)))
        self.assertIsNone(tep.terminal_default_mismatch(self.hermes_tree(mirror)))

    def test_a_hermes_whose_default_moved_is_a_mismatch_naming_both_figures(self):
        mirror = tep.preflight_terminal_mirror_seconds()
        message = tep.terminal_default_mismatch(self.hermes_tree(mirror + 1))
        self.assertIsNotNone(message)
        self.assertIn(f"{mirror + 1}s on this Hermes", message)
        self.assertIn(f"mirrors it as {mirror}s", message)

    def test_a_terminal_tool_the_regex_no_longer_matches_is_an_error_not_a_pass(self):
        with self.assertRaisesRegex(tep.PinError, "TERMINAL_TIMEOUT"):
            tep.hermes_terminal_default_seconds(self.hermes_tree(0, line='        "timeout": 180,\n'))


@unittest.skipUnless(HAS_HERMES, "needs hermes-agent importable")
class HermesTest(unittest.TestCase):
    """What a real Hermes resolves, in and out of an operator pod."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # /etc/hermes, which every image creates, and which Hermes falls back to.
        self.default_managed = self.tmp / "etc-hermes"
        self.default_managed.mkdir()
        self.managed = self.tmp / "managed"
        self.managed.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / ".env").write_text("A=1\n")
        env = {k: v for k, v in os.environ.items() if k != tep.MANAGED_DIR_ENV}
        for patcher in (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(managed_scope, "_DEFAULT_MANAGED_DIR", self.default_managed),
            mock.patch.object(managed_scope, "_under_pytest", lambda: False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def operator_pod(self, terminal=tep.BUILD_CHECK_TERMINAL):
        os.environ[tep.MANAGED_DIR_ENV] = str(self.managed)
        if terminal is not None:
            (self.managed / "config.yaml").write_text(tep.yaml.safe_dump({"terminal": terminal}))

    def build_check(self):
        with mock.patch.object(tep.sys, "stderr", new_callable=io.StringIO) as err:
            code = tep.build_check()
        return code, err.getvalue()

    def test_the_build_check_passes_on_this_hermes(self):
        code, err = self.build_check()
        self.assertEqual(0, code, err)

    def test_the_build_check_fails_when_the_sweep_cannot_list_profiles(self):
        with mock.patch.object(tep, "profile_homes", side_effect=ImportError("cannot import name '_PROFILE_ID_RE'")):
            code, err = self.build_check()
        self.assertEqual(1, code)
        self.assertIn("cannot list profiles", err)

    def test_the_build_check_fails_when_the_sweep_would_skip_a_named_profile(self):
        with mock.patch.object(tep, "profile_homes", side_effect=lambda root: [root]):
            code, err = self.build_check()
        self.assertEqual(1, code)
        self.assertIn("would not pin the named profile", err)

    def test_the_build_check_fails_when_the_preflight_mirror_disagrees_with_hermes(self):
        # The wiring: build_check returns 1 on the mismatch. The parse and the
        # mismatch text themselves are TerminalDefaultMirrorTest's, which CI
        # runs; the agreement with the shipped Hermes is the image build's
        # (deploy/docker/Dockerfile runs --build-check under it).
        with mock.patch.object(tep, "hermes_terminal_default_seconds", return_value=tep.preflight_terminal_mirror_seconds() + 1):
            code, err = self.build_check()
        self.assertEqual(1, code)
        self.assertIn("mirrors it as", err)

    def test_outside_the_operator_an_empty_etc_hermes_pins_nothing(self):
        self.assertEqual({}, tep.managed_terminal_env())
        self.assertFalse(tep.pin(self.home))
        self.assertEqual("A=1\n", (self.home / ".env").read_text())
        self.assertEqual(0, tep.sweep(self.home))

    def test_outside_the_operator_a_managed_terminal_block_is_still_copied(self):
        (self.default_managed / "config.yaml").write_text(tep.yaml.safe_dump({"terminal": tep.BUILD_CHECK_TERMINAL}))
        self.assertTrue(tep.pin(self.home))
        self.assertEqual("ssh", tep.resolved_scope(self.home)["TERMINAL_ENV"])

    def test_in_an_operator_pod_every_mapped_key_is_pinned_but_workspace_root(self):
        self.operator_pod()
        pinned = tep.managed_terminal_env()
        self.assertEqual("ssh", pinned["TERMINAL_ENV"])
        self.assertEqual(tep.BUILD_CHECK_TERMINAL["ssh_host"], pinned["TERMINAL_SSH_HOST"])
        self.assertEqual("2222", pinned["TERMINAL_SSH_PORT"])
        self.assertFalse(any("WORKSPACE" in key for key in pinned))

    def test_in_an_operator_pod_a_missing_or_wrong_managed_block_is_an_error(self):
        for terminal in (None, {"backend": "local"}):
            with self.subTest(terminal=terminal):
                (self.managed / "config.yaml").unlink(missing_ok=True)
                self.operator_pod(terminal)
                with self.assertRaises(tep.PinError):
                    tep.managed_terminal_env()

    def test_an_operator_managed_dir_that_is_missing_is_an_error(self):
        os.environ[tep.MANAGED_DIR_ENV] = str(self.tmp / "absent")
        with self.assertRaises(tep.PinError):
            tep.managed_terminal_env()

    def test_the_copy_makes_hermes_resolve_ssh(self):
        self.operator_pod()
        self.assertNotEqual("ssh", tep.resolved_scope(self.home).get("TERMINAL_ENV"))
        tep.pin(self.home)
        self.assertEqual("ssh", tep.resolved_scope(self.home)["TERMINAL_ENV"])

    def test_a_profile_config_key_that_outranks_the_copy_is_deleted_and_the_rest_kept(self):
        self.operator_pod()
        config = self.home / "config.yaml"
        config.write_text("# mine\nmodel: x\nterminal:\n  backend: local  # old\n  timeout: 60\n")
        config.chmod(0o640)
        self.assertTrue(tep.pin(self.home))
        self.assertEqual("ssh", tep.resolved_scope(self.home)["TERMINAL_ENV"])
        text = config.read_text()
        self.assertNotIn("backend", text)
        for kept in ("# mine", "model: x", "timeout: 60"):
            self.assertIn(kept, text)
        self.assertEqual(0o640, stat.S_IMODE(config.stat().st_mode))

    def test_a_profile_config_key_that_cannot_be_deleted_is_fatal_and_named(self):
        self.operator_pod()
        (self.home / "config.yaml").write_text("terminal:\n  backend: local\n")
        with mock.patch("utils.atomic_roundtrip_yaml_update", side_effect=OSError("read-only")):
            with self.assertRaisesRegex(tep.PinError, "cannot delete terminal.backend from"):
                tep.pin(self.home)

    def test_a_profile_config_hermes_cannot_parse_is_scope_unavailable(self):
        self.operator_pod()
        (self.home / "config.yaml").write_text("terminal: [unclosed\n")
        with self.assertRaises(tep.ScopeUnavailable):
            tep.pin(self.home)

    def test_an_env_hermes_cannot_read_is_fatal_not_skipped(self):
        self.operator_pod()
        (self.home / ".env").unlink()
        (self.home / ".env").mkdir()
        with self.assertRaises(tep.PinError) as caught:
            tep.pin(self.home)
        self.assertNotIsInstance(caught.exception, tep.ScopeUnavailable)

    @unittest.skipIf(os.geteuid() == 0, "root ignores the mode bits this test relies on")
    def test_a_profile_the_copy_cannot_be_written_to_is_fatal_even_if_it_resolves(self):
        self.operator_pod()
        (self.home / "config.yaml").write_text(tep.yaml.safe_dump({"terminal": tep.BUILD_CHECK_TERMINAL}))
        self.assertEqual("ssh", tep.resolved_scope(self.home)["TERMINAL_ENV"])
        self.home.chmod(0o555)
        self.addCleanup(self.home.chmod, 0o755)
        with self.assertRaises(tep.PinError) as caught:
            tep.pin(self.home)
        self.assertIn("cannot rewrite", str(caught.exception))

    def test_a_value_env_cannot_hold_is_left_out_only_outside_the_operator(self):
        terminal = {"backend": "docker", "docker_volumes": ["/a:/b"], "cwd": "~/work"}
        (self.default_managed / "config.yaml").write_text(tep.yaml.safe_dump({"terminal": terminal}))
        with mock.patch.object(tep.sys, "stderr", new_callable=io.StringIO):
            self.assertEqual({"TERMINAL_ENV": "docker"}, tep.managed_terminal_env())
        self.operator_pod({**tep.BUILD_CHECK_TERMINAL, "docker_volumes": ["/a:/b"]})
        with self.assertRaises(tep.PinError):
            tep.managed_terminal_env()

    def test_the_sweep_covers_named_profiles_and_skips_tombstones(self):
        self.operator_pod()
        live, deleted = self.home / "profiles" / "platform", self.home / "profiles" / "gone"
        live.mkdir(parents=True)
        deleted.mkdir()
        mark_named_profile_deleted(deleted)
        self.assertEqual([self.home, live], tep.profile_homes(self.home))
        self.assertEqual(0, tep.sweep(self.home))
        self.assertEqual("ssh", tep.resolved_scope(live)["TERMINAL_ENV"])


class DefinedKeyTest(unittest.TestCase):
    """The shapes agent.secret_scope.load_env_file reads as an assignment."""

    def test_assignments(self):
        for line, key in (
            ("KEY=v\n", "KEY"),
            ("export KEY=v\n", "KEY"),
            ("  KEY = v\n", "KEY"),
            ("export   KEY=v", "KEY"),
            ("KEY=\n", "KEY"),
        ):
            with self.subTest(line=line):
                self.assertEqual(key, tep.defined_key(line))

    def test_not_assignments(self):
        for line in ("# KEY=v\n", "   # KEY=v\n", "\n", "KEY\n", "=v\n"):
            with self.subTest(line=line):
                self.assertIsNone(tep.defined_key(line))


class PlainValueTest(unittest.TestCase):
    def test_accepts_what_the_operator_renders(self):
        for value in (PINNED["TERMINAL_SSH_HOST"], "ssh", "agent", "2222", "/etc/sandbox-ssh/id_ed25519"):
            with self.subTest(value=value):
                self.assertIsNotNone(tep.PLAIN_VALUE.fullmatch(value))

    def test_rejects_what_would_change_other_lines(self):
        for value in ("host\nTERMINAL_ENV=local", "a b", "x#y", '"quoted"', "${HOST}", ""):
            with self.subTest(value=value):
                self.assertIsNone(tep.PLAIN_VALUE.fullmatch(value))


if __name__ == "__main__":
    unittest.main()
