"""Unit tests for apply_ssh_shared_master.py and its verifier.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The applier's contract against miniature copies of the three Hermes files, and the verifier
against the same stubs patched and unpatched: it imports them as ``tools.*`` from the staged root.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_ssh_shared_master as verify
from apply_ssh_shared_master import (
    LIFECYCLE_ANCHOR, LIFECYCLE_RELATIVE, MARKER, RESULT_ANCHOR, RESULT_RELATIVE, SSH_CLEANUP_ANCHOR,
    SSH_INIT_ANCHOR, SSH_RELATIVE, apply,
)

# tools/environments/ssh.py at v2026.9.14: the two anchored regions, nothing else of the class.
SSH_STUB = (
    "import contextlib\n"
    "import hashlib\n"
    "import logging\n"
    "import subprocess\n"
    "from pathlib import Path\n"
    "\n"
    "logger = logging.getLogger(__name__)\n"
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
    + SSH_CLEANUP_ANCHOR
)

# tools/terminal_tool_lifecycle.py: _evict_environment_for_task and the names it uses.
LIFECYCLE_STUB = (
    "import contextlib\n"
    "from typing import Optional\n"
    "\n"
    "\n"
    "@contextlib.contextmanager\n"
    "def _quiet(what):\n"
    "    try:\n"
    "        yield\n"
    "    except Exception:\n"
    "        pass\n"
    "\n"
    "\n"
    "def _evict_environment_for_task(task_id: Optional[str]) -> None:\n"
    "    from tools.terminal_tool import (\n"
    "        _active_environments, _env_lock, _last_activity, _resolve_container_task_id,\n"
    "    )\n"
    "    keys = {_resolve_container_task_id(task_id)}\n"
    "    if task_id:\n"
    "        keys.add(task_id)\n"
    "    evicted = []\n"
    "    with _env_lock:\n"
    "        for key in keys:\n"
    "            env = _active_environments.pop(key, None)\n"
    "            _last_activity.pop(key, None)\n"
    "            if env is not None:\n"
    "                evicted.append(env)\n"
    + LIFECYCLE_ANCHOR
)

TERMINAL_TOOL_STUB = (
    "import threading\n"
    "\n"
    "_active_environments = {}\n"
    "_last_activity = {}\n"
    "_env_lock = threading.RLock()\n"
    "\n"
    "\n"
    "def _resolve_container_task_id(task_id):\n"
    '    return task_id or "default"\n'
)

# tools/terminal_tool_result.py: finalize_foreground_result down to the anchor and the JSON tail.
RESULT_STUB = (
    "import json\n"
    "\n"
    '_EXIT_CODE_HINTS = {124: "Exit 124: the command hit its timeout."}\n'
    "\n"
    "\n"
    "def _failure_hint(command, returncode, output, exit_note):\n"
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


def stage(ssh=SSH_STUB, lifecycle=LIFECYCLE_STUB, result=RESULT_STUB):
    root = Path(tempfile.mkdtemp())
    for rel in ("tools/__init__.py", "tools/environments/__init__.py"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("")
    (root / SSH_RELATIVE).write_text(ssh)
    (root / LIFECYCLE_RELATIVE).write_text(lifecycle)
    (root / RESULT_RELATIVE).write_text(result)
    (root / "tools/terminal_tool.py").write_text(TERMINAL_TOOL_STUB)
    return root


def run_verifier(root):
    import sys
    with mock.patch.object(verify, "HERMES", root), mock.patch.object(verify, "FAILURES", []), \
            mock.patch.object(sys, "path", list(sys.path)):
        rc = verify.main()
        return rc, list(verify.FAILURES)


class ApplierTest(unittest.TestCase):
    def test_the_three_files_are_patched(self):
        root = stage()
        apply(root)
        ssh = (root / SSH_RELATIVE).read_text()
        self.assertIn("self._shared_master = not probe_only", ssh)
        self.assertIn("def close_master(self):", ssh)
        self.assertNotIn(SSH_CLEANUP_ANCHOR, ssh)
        self.assertIn('getattr(env, "close_master", lambda: None)()', (root / LIFECYCLE_RELATIVE).read_text())
        self.assertIn('returncode == 255 and not (result or {}).get("cwd_observed")', (root / RESULT_RELATIVE).read_text())
        for rel in (SSH_RELATIVE, LIFECYCLE_RELATIVE, RESULT_RELATIVE):
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
        self.assertNotIn(MARKER, (root / RESULT_RELATIVE).read_text())


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
        self.assertIn("eviction called ['cleanup']", joined)
        self.assertIn("carries no hint", joined)

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
