"""The two file lists of .github/workflows/installer-tests-bash-floor.yml.

The job runs the test files of `INSTALLER_TEST_FILES` under /bin/bash 3.2, and `paths` names
the files whose change starts it. People keep both lists by hand. These tests hold them against
the tree, so that a new installer test file is not left out of the job in silence, and so
that a script the job's tests run stays in `paths`.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "installer-tests-bash-floor.yml"
# What counts as a new installer test file here. A test file with another name is not seen.
_INSTALLER_TEST_GLOB = "tests/test_installer_*.py"
# The files of the job that the glob does not match: the tests of the three front-door
# scripts, of lifecycle.sh and of scripts/dev/repeat_under_load.sh, and the helm retry
# tests of hack/ci-deploy.sh.
_ALSO_IN_THE_JOB = {
    "tests/test_install_script.py",
    "tests/test_upgrade_script.py",
    "tests/test_uninstall_script.py",
    "tests/test_lifecycle_script.py",
    "tests/test_repeat_under_load.py",
    "tests/test_ci_deploy_helm_retry.py",
}
# The scripts that the job's test files run and that `paths` names one by one. A change
# to one of them must start the job. The scripts under scripts/installer/ and
# terraform/examples/full-install/ are in `paths` through a pattern, which these tests do
# not read.
_SCRIPTS_OF_THE_JOB = {
    "install.sh",
    "upgrade.sh",
    "uninstall.sh",
    "scripts/release/common.sh",
    "scripts/confirm_agent_image.sh",
    "scripts/dev/repeat_under_load.sh",
    "hack/ci-deploy.sh",
}
# An installer test file that the job does not run: its path as the glob gives it
# ("tests/test_installer_x.py"), and the reason.
_NOT_IN_THE_JOB = {}


def _workflow():
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow):
    # YAML 1.1 reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _job_files(workflow):
    return workflow["env"]["INSTALLER_TEST_FILES"].split()


class BashFloorFileListsTest(unittest.TestCase):
    def test_each_installer_test_file_is_in_the_job_or_is_a_named_exception(self):
        job_files = set(_job_files(_workflow()))
        installer_tests = {str(p.relative_to(_REPO_ROOT)) for p in _REPO_ROOT.glob(_INSTALLER_TEST_GLOB)}
        self.assertTrue(installer_tests, f"{_INSTALLER_TEST_GLOB} matched no file")
        self.assertEqual(
            set(),
            (installer_tests | _ALSO_IN_THE_JOB) - job_files - set(_NOT_IN_THE_JOB),
            f"add each file to INSTALLER_TEST_FILES and to paths in {_WORKFLOW.name}, "
            "or to _NOT_IN_THE_JOB here with the reason",
        )
        self.assertEqual(set(), set(_NOT_IN_THE_JOB) & job_files, "an exception that the job runs")
        self.assertEqual(
            set(), set(_NOT_IN_THE_JOB) - installer_tests, f"an exception that {_INSTALLER_TEST_GLOB} does not match"
        )

    def test_each_file_of_the_job_exists_and_a_change_to_it_starts_the_job(self):
        workflow = _workflow()
        job_files = _job_files(workflow)
        paths = _triggers(workflow)["pull_request"]["paths"]
        self.assertEqual(len(job_files), len(set(job_files)), "a file is in INSTALLER_TEST_FILES two times")
        self.assertEqual(
            [], [entry for entry in paths if entry.startswith("!")], "a ! entry of paths can take a listed file out"
        )
        for file in job_files:
            with self.subTest(file=file):
                self.assertTrue((_REPO_ROOT / file).is_file(), "INSTALLER_TEST_FILES names a file that does not exist")
                # The file itself, as text. This test does not read the patterns of `paths`:
                # GitHub Actions has its own rules for `*`, `**` and `!`.
                self.assertIn(file, paths, "list the file itself in paths; a pattern that covers it is not read here")

    def test_a_change_to_a_script_of_the_job_starts_the_job(self):
        paths = _triggers(_workflow())["pull_request"]["paths"]
        for script in sorted(_SCRIPTS_OF_THE_JOB):
            with self.subTest(script=script):
                self.assertTrue((_REPO_ROOT / script).is_file(), "the script does not exist")
                self.assertIn(script, paths, "list the script itself in paths; a pattern that covers it is not read here")

    def test_the_push_trigger_has_the_same_paths_as_the_pull_request_trigger(self):
        triggers = _triggers(_workflow())
        self.assertEqual(triggers["pull_request"]["paths"], triggers["push"]["paths"])


if __name__ == "__main__":
    unittest.main()
