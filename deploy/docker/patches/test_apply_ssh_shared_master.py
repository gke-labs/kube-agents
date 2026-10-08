"""Unit tests for apply_ssh_shared_master.py and its verifier.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The applier's contract against miniature copies of the two Hermes files, and the verifier
against the same stubs patched and unpatched: it imports them as ``tools.*`` from the staged root.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_ssh_shared_master as verify
from apply_ssh_shared_master import (
    MARKER, RESULT_ANCHOR, RESULT_RELATIVE, SSH_ARGV_ANCHOR, SSH_CLEANUP_ANCHOR, SSH_INIT_ANCHOR, SSH_RELATIVE,
    apply,
)

# tools/environments/ssh.py at v2026.9.14: the three anchored regions, plus the two helpers
# _build_ssh_command needs so the verifier can call it; nothing else of the class.
SSH_STUB = (
    "import contextlib\n"
    "import hashlib\n"
    "import logging\n"
    "import subprocess\n"
    "from pathlib import Path\n"
    "\n"
    "logger = logging.getLogger(__name__)\n"
    "_SSH_MULTIPLEX = True\n"
    "\n"
    "\n"
    "class SSHEnvironment:\n"
    "    def __init__(self, host, user, port=22, probe_only=False):\n"
    "        self.host, self.user, self.port = host, user, port\n"
    "        self._sync_manager = None\n"
    '        socket_key = f"{user}@{host}:{port}"\n'
    "        if probe_only:\n"
    '            socket_key = f"{socket_key}:probe:x"\n'
    + SSH_INIT_ANCHOR +
    '        self.control_socket = Path("/tmp/hermes-ssh") / f"{_socket_id}.sock"\n'
    "\n"
    "    def _control_sockets(self):\n"
    "        plain = Path(self.control_socket)\n"
    '        siblings = sorted(plain.parent.glob(f"{plain.stem[:8]}*.sock")) if plain.parent.is_dir() else []\n'
    "        return [plain, *(s for s in siblings if s != plain)]\n"
    "\n"
    "    def _control_socket_for(self, send_env):\n"
    "        return Path(self.control_socket)\n"
    "\n"
    "    def _target_flags(self, port_flag):\n"
    "        flags = [port_flag, str(self.port)] if self.port != 22 else []\n"
    '        return flags + (["-i", self.key_path] if self.key_path else [])\n'
    "\n"
    "    def _build_ssh_command(self, extra_args=None, send_env=()):\n"
    "        send_env = tuple(sorted(send_env))\n"
    '        cmd = ["ssh"]\n'
    "        if _SSH_MULTIPLEX:\n"
    '            cmd.extend(["-o", f"ControlPath={self._control_socket_for(send_env)}",\n'
    '                        "-o", "ControlMaster=auto", "-o", "ControlPersist=300"])\n'
    + SSH_ARGV_ANCHOR +
    '        cmd.extend(arg for name in send_env for arg in ("-o", f"SendEnv={name}"))\n'
    '        cmd.extend(self._target_flags("-p"))\n'
    "        cmd.extend(extra_args or [])\n"
    '        cmd.append(f"{self.user}@{self.host}")\n'
    "        return cmd\n"
    "\n"
    + SSH_CLEANUP_ANCHOR
)

# tools/terminal_tool_result.py: finalize_foreground_result down to the anchor and the JSON tail.
RESULT_STUB = (
    "import json\n"
    "\n"
    '_EXIT_CODE_HINTS = {124: "Exit 124: the command hit its timeout."}\n'
    "\n"
    "\n"
    "def _failure_hint(command, returncode, output, exit_note):\n"
    '    if "Permission denied" in output:\n'
    '        return "Permission denied. Check ownership/mode of the target path."\n'
    "    return _EXIT_CODE_HINTS.get(returncode)\n"
    "\n"
    "\n"
    "def finalize_foreground_result(*, command, result, env, env_type, effective_task_id, task_id,\n"
    "                               session_id, session_key, workdir, command_cwd, approval_note):\n"
    '    output = result.get("output", "")\n'
    '    returncode = result.get("returncode", 0)\n'
    "    exit_note = None\n"
    + RESULT_ANCHOR +
    '    result_dict = {"output": output, "exit_code": returncode, "error": None}\n'
    "    if failure_hint:\n"
    '        result_dict["hint"] = failure_hint\n'
    "    return json.dumps(result_dict)\n"
)


def stage(ssh=SSH_STUB, result=RESULT_STUB):
    root = Path(tempfile.mkdtemp())
    for rel in ("tools/__init__.py", "tools/environments/__init__.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("")
    (root / SSH_RELATIVE).write_text(ssh)
    (root / RESULT_RELATIVE).write_text(result)
    return root


def run_verifier(root):
    import sys
    with mock.patch.object(verify, "HERMES", root), mock.patch.object(verify, "FAILURES", []), \
            mock.patch.object(sys, "path", list(sys.path)):
        rc = verify.main()
        return rc, list(verify.FAILURES)


class ApplierTest(unittest.TestCase):
    def test_the_two_files_are_patched(self):
        root = stage()
        apply(root)
        ssh = (root / SSH_RELATIVE).read_text()
        self.assertIn("self._shared_master = not probe_only", ssh)
        self.assertIn("def close_master(self):", ssh)
        self.assertIn('cmd.extend(["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"])', ssh)
        self.assertNotIn(SSH_CLEANUP_ANCHOR, ssh)
        self.assertIn('returncode == 255 and failure_hint is None and not (result or {}).get("cwd_observed")',
                      (root / RESULT_RELATIVE).read_text())
        for rel in (SSH_RELATIVE, RESULT_RELATIVE):
            self.assertIn(MARKER, (root / rel).read_text(), rel)

    def test_a_second_apply_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_moved_anchor_is_refused_before_any_file_changes(self):
        root = stage(result=RESULT_STUB.replace(
            "failure_hint = _failure_hint(command, returncode, output, exit_note)",
            "failure_hint = _failure_hint(command, returncode, output)"))
        with self.assertRaises(SystemExit):
            apply(root)
        for rel in (SSH_RELATIVE, RESULT_RELATIVE):
            self.assertNotIn(MARKER, (root / rel).read_text(), rel)


@unittest.skipUnless(shutil.which("ssh") or Path("/usr/bin/ssh").exists(), "the verifier resolves the argv with ssh -G")
class VerifierTest(unittest.TestCase):
    def test_patched_stubs_pass(self):
        root = stage()
        apply(root)
        rc, failures = run_verifier(root)
        self.assertEqual((rc, failures), (0, []))

    def test_unpatched_stubs_fail_on_every_check(self):
        rc, failures = run_verifier(stage())
        self.assertEqual(rc, 1)
        joined = "\n".join(failures)
        self.assertIn("does not carry the patch marker", joined)
        self.assertIn("no close_master()", joined)
        self.assertIn("carries no hint", joined)

    def test_an_argv_without_the_keep_alive_pair_fails(self):
        root = stage()
        apply(root)
        path = root / SSH_RELATIVE
        path.write_text(path.read_text().replace('"-o", "ServerAliveInterval=15", ', ""))
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        # What ssh falls back to is the machine's (a ~/.ssh/config may set its own), so only the
        # keyword and the expected value are pinned.
        self.assertRegex("\n".join(failures), r"ssh resolves serveraliveinterval to \S+, expected 15")

    def test_an_argv_without_the_count_fails(self):
        root = stage()
        apply(root)
        path = root / SSH_RELATIVE
        path.write_text(path.read_text().replace(', "-o", "ServerAliveCountMax=3"', ""))
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertRegex("\n".join(failures), r"ssh resolves serveralivecountmax to \S+, expected 3")

    def test_an_earlier_copy_of_the_interval_fails(self):
        # An upstream `-o ServerAliveInterval=N` placed before the anchor is earlier in argv and
        # wins under first-value-wins, so the gate refuses any second copy whatever its value.
        root = stage(ssh=SSH_STUB.replace(
            '        cmd = ["ssh"]\n', '        cmd = ["ssh", "-o", "ServerAliveInterval=5"]\n'))
        apply(root)
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertIn("ssh resolves serveraliveinterval to 5, expected 15", "\n".join(failures))

    def test_every_spelling_of_an_earlier_copy_fails(self):
        # ssh also takes the option glued to -o, after `-o=`, bundled behind other short flags, with
        # the keyword in any case, and with a space instead of `=`; the gate asks ssh -G what it
        # resolved, so each spelling lands as the earlier, winning value.
        for spelling, expect in (('"-oServerAliveInterval=0"', "serveraliveinterval to 0"),
                                 ('"-o=ServerAliveInterval=5"', "serveraliveinterval to 5"),
                                 ('"-To", "ServerAliveInterval=5"', "serveraliveinterval to 5"),
                                 ('"-o", "serveraliveinterval=5"', "serveraliveinterval to 5"),
                                 ('"-o", "ServerAliveInterval 5"', "serveraliveinterval to 5"),
                                 ('"-4oserveralivecountmax=9"', "serveralivecountmax to 9")):
            with self.subTest(spelling=spelling):
                root = stage(ssh=SSH_STUB.replace('        cmd = ["ssh"]\n', f'        cmd = ["ssh", {spelling}]\n'))
                apply(root)
                rc, failures = run_verifier(root)
                self.assertEqual(rc, 1, spelling)
                self.assertIn(f"ssh resolves {expect}, expected", "\n".join(failures), spelling)

    def test_a_glued_or_bundled_dash_f_fails(self):
        # Where the image's drop-in is installed the gate sees its SendEnv vanish; elsewhere (this
        # suite on a developer machine) it falls back to the flag itself, in any bundling.
        for spelling in ('"-F/dev/null"', '"-4F", "/dev/null"', '"-vF/dev/null"'):
            with self.subTest(spelling=spelling):
                root = stage(ssh=SSH_STUB.replace('        cmd = ["ssh"]\n', f'        cmd = ["ssh", {spelling}]\n'))
                apply(root)
                rc, failures = run_verifier(root)
                self.assertEqual(rc, 1, spelling)
                self.assertIn("-F", "\n".join(failures), spelling)

    def test_a_renamed_probe_only_parameter_fails_the_gate(self):
        # The applier's __init__ anchor is the _socket_id line, which an upstream rename of
        # probe_only leaves intact; the inserted mark then raises NameError on every construction.
        root = stage(ssh=SSH_STUB.replace("probe_only=False", "probe=False").replace("if probe_only:", "if probe:"))
        apply(root)
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertIn("reads probe_only, which its function no longer binds", "\n".join(failures))

    def test_a_hint_that_overrides_the_upstream_one_fails(self):
        root = stage()
        apply(root)
        path = root / RESULT_RELATIVE
        path.write_text(path.read_text().replace("returncode == 255 and failure_hint is None and", "returncode == 255 and"))
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertIn("Permission denied hint lost", "\n".join(failures))

    def test_a_cleanup_that_still_closes_the_master_fails(self):
        root = stage()
        apply(root)
        path = root / SSH_RELATIVE
        path.write_text(path.read_text().replace(
            'if not getattr(self, "_shared_master", True):\n            self.close_master()',
            'self.close_master()'))
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertIn("a shared environment's cleanup() still closes the master", "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
