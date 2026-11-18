"""The report files of install.sh, upgrade.sh and uninstall.sh, in tests.

Each script writes a JSON report to one fixed file under /tmp. A script that a test only sources
writes it too when a command fails, through its ERR trap. `tests/testing/common.py` names a file
of the test process for each script: in `os.environ`, in `get_isolated_test_env`, and through
`installer_report_env()` for an environment that a test builds from nothing. The lock file of
uninstall.sh, which the script takes also when it is only sourced, is in the same folder.
"""

import ast
import json
import os
import pathlib
import re
import subprocess
import unittest
from unittest import mock

from tests.testing.common import (
    INSTALLER_REPORT_FILE_ENV_VARS,
    UNINSTALL_LOCK_FILE_ENV,
    UNINSTALL_LOCK_FILE_NAME,
    get_isolated_test_env,
    installer_report_env,
    uninstall_lock_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
# Where a real run writes its report.
_REAL_REPORT_DIR = pathlib.Path("/tmp")
_SCRIPT_OF_VARIABLE = {
    "KUBE_AGENTS_INSTALL_REPORT_FILE": "install.sh",
    "KUBE_AGENTS_UPGRADE_REPORT_FILE": "upgrade.sh",
    "KUBE_AGENTS_UNINSTALL_REPORT_FILE": "uninstall.sh",
}
# The test modules that source or run one of the three scripts, and the two forms that the guard
# knows them by: a script constant in a shell body's `source`, or after `"bash", str(`.
_MODULES_THAT_RUN_A_SCRIPT = {
    "test_install_script.py",
    "test_installer_bitbucket.py",
    "test_installer_ghes.py",
    "test_installer_gitlab.py",
    "test_installer_gitops_record.py",
    "test_uninstall_script.py",
    "test_upgrade_script.py",
}
_RUNS_A_SCRIPT = re.compile(
    r'source "\{\w*(?:INSTALL|UPGRADE|UNINSTALL)_SH\}"|"bash", str\(\w*(?:INSTALL|UPGRADE|UNINSTALL)_SH\b'
)
# The calls that start a child with an `env=` argument, and the helper of
# tests/test_install_script.py that takes the environment as its second argument.
_CHILD_STARTERS = ("run", "Popen", "check_output", "check_call", "call")
_BASH_RUNNER = "_run_installer_bash"
_SHARED_HELPERS = ("installer_report_env", "get_isolated_test_env")


class SharedReportFilesTest(unittest.TestCase):
    def _assert_the_script_reads(self, variable):
        """Held by the source: the script takes its report file from `variable`, then from /tmp."""
        real_file = _REAL_REPORT_DIR / INSTALLER_REPORT_FILE_ENV_VARS[variable]
        script = _REPO_ROOT / _SCRIPT_OF_VARIABLE[variable]
        self.assertIn(f'  local report_file="${{{variable}:-{real_file}}}"\n', script.read_text())

    def test_each_script_has_a_report_file_in_one_folder_that_is_not_the_real_one(self):
        files = installer_report_env()
        self.assertEqual(set(files), set(_SCRIPT_OF_VARIABLE))
        folders = {pathlib.Path(path).parent for path in files.values()}
        self.assertEqual(len(folders), 1, files)
        folder = folders.pop()
        self.assertTrue(folder.is_dir(), folder)
        self.assertNotEqual(folder.resolve(), _REAL_REPORT_DIR.resolve())
        self.assertEqual(installer_report_env(), files, "a second call names the same files")

    def test_the_variables_and_file_names_are_the_ones_the_scripts_have(self):
        self.assertEqual(set(INSTALLER_REPORT_FILE_ENV_VARS), set(_SCRIPT_OF_VARIABLE))
        for variable in _SCRIPT_OF_VARIABLE:
            with self.subTest(variable=variable):
                self._assert_the_script_reads(variable)

    def test_the_lock_file_of_uninstall_sh_is_in_the_folder_of_the_report_files(self):
        """uninstall.sh takes its lock also when a test only sources it."""
        lock = pathlib.Path(uninstall_lock_env()[UNINSTALL_LOCK_FILE_ENV])
        self.assertEqual(lock.name, UNINSTALL_LOCK_FILE_NAME)
        self.assertEqual({lock.parent}, {pathlib.Path(path).parent for path in installer_report_env().values()})

    def test_the_test_process_and_the_isolated_environment_have_them_and_an_override_wins(self):
        for variable, path in {**installer_report_env(), **uninstall_lock_env()}.items():
            with self.subTest(variable=variable):
                # For a child that inherits the environment of the test process.
                self.assertEqual(os.environ[variable], path)
                self.assertEqual(get_isolated_test_env()[variable], path)
                # A value that the developer's shell exports does not reach the child.
                with mock.patch.dict(os.environ, {variable: str(_REAL_REPORT_DIR / "from-the-shell.json")}):
                    self.assertEqual(get_isolated_test_env()[variable], path)
                own = get_isolated_test_env(overrides={variable: "/a/file/of/the/test.json"})
                self.assertEqual(own[variable], "/a/file/of/the/test.json")

    def test_a_sourced_script_whose_command_fails_writes_the_file_of_this_process(self):
        for variable, script in _SCRIPT_OF_VARIABLE.items():
            with self.subTest(script=script):
                env = get_isolated_test_env()
                # Checks before the run. Without the variable, with the real folder in it, or
                # with a script that reads another variable, the run would replace the report
                # of a real run. uninstall.sh takes its lock also when it is only sourced, so
                # the lock file must be one of this process too.
                report = pathlib.Path(env[variable])
                self.assertNotEqual(report.parent.resolve(), _REAL_REPORT_DIR.resolve())
                self.assertEqual(pathlib.Path(env[UNINSTALL_LOCK_FILE_ENV]).parent, report.parent)
                self._assert_the_script_reads(variable)
                report.unlink(missing_ok=True)
                body = (
                    f'source "{_INSTALLER_COMMON}"\n'
                    f'KUBE_AGENTS_SOURCE_ONLY=true source "{_REPO_ROOT / script}"\n'
                    "false\n"
                )
                proc = subprocess.run(
                    ["bash", "-c", body], capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT), timeout=60
                )
                self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
                self.assertEqual(json.loads(report.read_text())["status"], "FAILED", proc.stderr + proc.stdout)


def _environments_from_nothing(path):
    """The line of each child start in `path` whose environment is a dict display with no shared helper in it.

    Seen: `env={...}`, the environment argument of `_run_installer_bash`, and a name in either
    place where `name = {...}` is in the same function. Not seen: an environment that is built in
    another way (a call, a method that returns a dict, os.execve). A dict that gets the files in a
    later statement is reported too; it then needs `**installer_report_env()` in the display.
    """
    lines = []
    for func in ast.walk(ast.parse(path.read_text())):
        if not isinstance(func, ast.FunctionDef):
            continue
        displays = {
            target.id: node.value
            for node in ast.walk(func)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if called not in _CHILD_STARTERS and called != _BASH_RUNNER:
                continue
            environments = [keyword.value for keyword in node.keywords if keyword.arg == "env"]
            if called == _BASH_RUNNER:
                environments += node.args[1:2]
            for value in environments:
                value = displays.get(value.id) if isinstance(value, ast.Name) else value
                if not isinstance(value, ast.Dict):
                    continue
                unpacked = [v for k, v in zip(value.keys, value.values) if k is None and isinstance(v, ast.Call)]
                if not any(getattr(v.func, "id", getattr(v.func, "attr", "")) in _SHARED_HELPERS for v in unpacked):
                    lines.append(node.lineno)
    return sorted(set(lines))


class ModulesThatRunTheScriptsTest(unittest.TestCase):
    """A child that inherits the environment, or gets it from `get_isolated_test_env`, has the
    report files. The one form that does not is a bare dict, and this class looks for it.

    Its limits: it knows a module by the two forms of `_RUNS_A_SCRIPT`, and an environment by the
    forms that `_environments_from_nothing` names. A module or an environment in another form is
    not read.
    """

    def test_the_guard_reads_the_modules_that_run_the_scripts(self):
        found = {p.name for p in _TESTS_DIR.glob("test_*.py") if _RUNS_A_SCRIPT.search(p.read_text())}
        self.assertEqual(found - {pathlib.Path(__file__).name}, _MODULES_THAT_RUN_A_SCRIPT)

    def test_an_environment_built_from_nothing_takes_the_shared_report_files(self):
        for name in sorted(_MODULES_THAT_RUN_A_SCRIPT):
            with self.subTest(module=name):
                self.assertEqual(
                    _environments_from_nothing(_TESTS_DIR / name),
                    [],
                    f"{name}: add **installer_report_env() to the env dict on these lines (and "
                    "**uninstall_lock_env() for a child of uninstall.sh), or build the environment "
                    "with get_isolated_test_env",
                )


if __name__ == "__main__":
    unittest.main()
