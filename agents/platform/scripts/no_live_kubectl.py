"""Test support: keep a test module from reaching a cluster through kubectl.

`gitops_workspace` reads the repository lists from the mounted state file and,
when there is none, runs `kubectl get configmap <agent>-gitops-state`. A test
that leaves that read unstubbed used to run the real kubectl with the
developer's default kubeconfig. `install()` closes that for a whole test
module, three ways:

- `KUBECONFIG` is an empty temporary file, so no kubectl finds a cluster;
- a stand-in `kubectl` is first on `PATH`: it records its arguments and fails;
- the state read's own kubectl call fails inside the process, with the error a
  kubectl that cannot reach a cluster gives. The tests see the unreadable state
  CI has always given them, and start no process for it. A test that wants
  another answer patches `subprocess.run` or points `GITOPS_STATE_PATH` at a
  file; a kubectl of its own on `PATH` does not answer this read.

`Guard.uninstall()` restores all three, and raises when the stand-in was
called: a test, or a code path it reached, tried to run kubectl.

The stand-in and the empty kubeconfig reach a kubectl started with the
environment the guard set. A call that passes an `env=` of its own, clears
`os.environ`, or names kubectl by an absolute path gets neither, and the guard
does not see it.

`install()` while a guard is in force returns that guard, and only the last
`uninstall()` takes it down: unittest runs a module's fixtures again around a
suite that one of the module's own tests runs.

    def setUpModule():
        global _NO_KUBECTL
        _NO_KUBECTL = no_live_kubectl.install()

    def tearDownModule():
        _NO_KUBECTL.uninstall()

Used by tests only; nothing at runtime imports it.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

KUBECTL = "kubectl"
KUBECONFIG_ENV = "KUBECONFIG"
PATH_ENV = "PATH"
GUARDED_ENV = (KUBECONFIG_ENV, PATH_ENV)

# The guard's temporary directory and the names inside it.
_DIR_PREFIX = "no-live-kubectl-"
_KUBECONFIG_FILE = "kubeconfig"
_CALLS_FILE = "calls"
_BIN_DIR = "bin"
# A state mount that does not exist, for `assert_in_force`.
_NO_MOUNT_DIR = "no-such-mount"

# The stand-in's exit code: not one kubectl itself returns, so a test that
# reads it knows which of the two answered.
STAND_IN_EXIT_CODE = 97
STAND_IN_MESSAGE = "kubectl is not available in unit tests (no_live_kubectl)"
STAND_IN_MODE = 0o755
_STAND_IN_SCRIPT = """#!/bin/sh
printf '%s\\n' "$*" >> "{calls}"
echo "{message}" >&2
exit {code}
"""

# The one kubectl call answered in the process: gitops_workspace's read of the
# state ConfigMap, which it names from this variable, else by this default.
# Every other kubectl call found on PATH goes to the stand-in and fails the
# module.
STATE_READ_COMMAND = (KUBECTL, "get", "configmap")
STATE_CONFIGMAP_ENV = "GITOPS_STATE_CONFIGMAP"
STATE_CONFIGMAP_DEFAULT = "platform-agent-gitops-state"
STATE_PATH_ENV = "GITOPS_STATE_PATH"
# What kubectl prints with no cluster to reach, which is the read CI gives.
STATE_READ_ERROR = "The connection to the server localhost:8080 was refused - did you specify the right host or port?"
STATE_READ_EXIT_CODE = 1

# How many recorded calls the failure message quotes.
REPORTED_CALLS = 5

# The guard in force, if any; `install()` hands it to every caller.
_ACTIVE = None


def _is_state_read(cmd) -> bool:
    if not isinstance(cmd, (list, tuple)) or len(cmd) <= len(STATE_READ_COMMAND):
        return False
    name = os.environ.get(STATE_CONFIGMAP_ENV, STATE_CONFIGMAP_DEFAULT)
    return tuple(cmd[: len(STATE_READ_COMMAND)]) == STATE_READ_COMMAND and cmd[len(STATE_READ_COMMAND)] == name


class Guard:
    """The three barriers, from construction until the last `uninstall()`."""

    def __init__(self):
        self._dir = Path(tempfile.mkdtemp(prefix=_DIR_PREFIX))
        self.kubeconfig = self._dir / _KUBECONFIG_FILE
        self.kubeconfig.touch()
        self._calls = self._dir / _CALLS_FILE
        self._calls.touch()
        bin_dir = self._dir / _BIN_DIR
        bin_dir.mkdir()
        self.stand_in = bin_dir / KUBECTL
        self.stand_in.write_text(
            _STAND_IN_SCRIPT.format(calls=self._calls, message=STAND_IN_MESSAGE, code=STAND_IN_EXIT_CODE),
            encoding="utf-8",
        )
        self.stand_in.chmod(STAND_IN_MODE)
        self._saved_env = {name: os.environ.get(name) for name in GUARDED_ENV}
        os.environ[KUBECONFIG_ENV] = str(self.kubeconfig)
        os.environ[PATH_ENV] = str(bin_dir) + os.pathsep + os.environ.get(PATH_ENV, "")
        self._real_run = subprocess.run
        subprocess.run = self._run
        self._holders = 0

    def _run(self, *args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if _is_state_read(cmd):
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(STATE_READ_EXIT_CODE, cmd, output="", stderr=STATE_READ_ERROR)
            return subprocess.CompletedProcess(cmd, STATE_READ_EXIT_CODE, stdout="", stderr=STATE_READ_ERROR)
        return self._real_run(*args, **kwargs)

    def calls(self) -> list[str]:
        """The arguments of each call the stand-in took, oldest first."""
        return self._calls.read_text(encoding="utf-8").splitlines()

    def uninstall(self) -> None:
        global _ACTIVE
        if _ACTIVE is not self:
            return
        self._holders -= 1
        if self._holders > 0:
            return
        _ACTIVE = None
        subprocess.run = self._real_run
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        calls = self.calls()
        shutil.rmtree(self._dir, ignore_errors=True)
        if calls:
            shown = "; ".join(f"{KUBECTL} {call}" for call in calls[:REPORTED_CALLS])
            raise AssertionError(
                f"{len(calls)} kubectl call(s) left this test module for the kubectl on PATH. "
                f"Stub the call in the test that makes it. First calls: {shown}"
            )


def install() -> Guard:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = Guard()
    _ACTIVE._holders += 1
    return _ACTIVE


def assert_in_force(test, guard: Guard) -> None:
    """For one test in each guarded module: the barriers are up, and the state
    read with no mounted file is unreadable without a kubectl process."""
    import gitops_workspace

    test.assertEqual(os.environ.get(KUBECONFIG_ENV), str(guard.kubeconfig))
    test.assertEqual(guard.kubeconfig.read_text(encoding="utf-8"), "")
    test.assertEqual(shutil.which(KUBECTL), str(guard.stand_in))
    # No mount at all, also where the host has the operator's real one.
    no_mount = guard.kubeconfig.parent / _NO_MOUNT_DIR
    before = guard.calls()
    state_path = str(no_mount / gitops_workspace.MANAGED_REPOS_KEY)
    with patch.object(gitops_workspace, "GITOPS_STATE_DIR", str(no_mount)), patch.dict(
        os.environ, {STATE_PATH_ENV: state_path}
    ), test.assertRaises(RuntimeError):
        gitops_workspace._read_state_text(gitops_workspace.MANAGED_REPOS_KEY)
    test.assertEqual(guard.calls(), before)
