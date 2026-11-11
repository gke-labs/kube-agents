"""The exported grader tree carries no `.venv` that `uv sync` cannot build over.

`hack/ci-eval-pr.sh` grades a pull request with main's `bench/` at
PULL_BASE_SHA, exported by `pin_grader` into a scratch directory and synced
there with `uv sync`. A `.venv` entry the export carries -- `bench/.venv` was
committed as a symbolic link to a developer's machine at c418002ff -- is a
dangling link in the scratch tree, and `uv sync` fails on it twice with
"failed to create directory ... File exists (os error 17)" before any case
runs. The guard under test, `drop_stray_grader_venv`, removes a `.venv` that
is not a real directory from the exported tree and says so; `pin_grader`
calls it, so the uv step never sees the link. This runs the real functions,
lifted from the script, against a throwaway repository that commits the link.
"""

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
GUARD = "drop_stray_grader_venv"
EXPORT = "pin_grader"
CONSTANTS = ("EVAL_GRADER_TREE", "EVAL_GRADER_CASES_DIR", "EVAL_GRADER_VENV_DIR")
# What the committed link pointed at: a path on nobody's runner.
STRAY_TARGET = "/Users/someone/ws/bench/.venv"
# What `uv sync` does first in a tree without a venv, and what failed on the
# link: create the directory. The stub stands in for the network fetch.
UV_STUB = 'uv() { [ "$1" = sync ] && mkdir "${PWD}/.venv"; }'
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def lifted(name: str) -> str:
    """A top-level shell function as written, lifted from the script."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{[^\n]*\n.*?^\}}$", src, re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name}() not found in {SCRIPT}")
    return match.group(0)


def lifted_constant(name: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^readonly {name}=.*$", src, re.MULTILINE)
    if match is None:
        raise AssertionError(f"readonly {name} not found in {SCRIPT}")
    return match.group(0)


def run_bash(body: str) -> subprocess.CompletedProcess:
    prelude = "\n".join([*(lifted_constant(c) for c in CONSTANTS), lifted(GUARD), lifted(EXPORT)])
    return subprocess.run(
        ["bash", "-c", "set -uo pipefail\n" + prelude + "\n" + body],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")},
    )


def in_repo(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_IDENTITY},
    )
    return proc.stdout.strip()


def repo_with_committed_link(root: pathlib.Path) -> tuple[pathlib.Path, str]:
    """A repository whose bench/ commits a case and a `.venv` link, as main
    did at c418002ff; returns it and the commit."""
    repo = root / "repo"
    (repo / "bench" / "tasks" / "a-case").mkdir(parents=True)
    (repo / "bench" / "tasks" / "a-case" / "task.yaml").write_text("id: a-case\n")
    (repo / "bench" / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "bench" / ".venv").symlink_to(STRAY_TARGET)
    in_repo(repo, "init", "-q")
    in_repo(repo, "add", "-A")
    in_repo(repo, "commit", "-q", "-m", "bench with a venv link")
    return repo, in_repo(repo, "rev-parse", "HEAD")


class ExportTest(unittest.TestCase):
    def test_a_committed_venv_link_is_dropped_and_uv_sync_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            repo, sha = repo_with_committed_link(root)
            out = root / "scratch"
            out.mkdir()
            proc = run_bash(
                "\n".join(
                    [
                        UV_STUB,
                        f'{EXPORT} "{repo}" "{sha}" "{out}" || exit 7',
                        f'(cd "{out}/bench" && uv sync --quiet) || exit 8',
                        "echo synced",
                    ]
                )
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("synced", proc.stdout)
            self.assertTrue((out / "bench" / ".venv").is_dir(), "uv's directory stands where the link was")
            self.assertFalse((out / "bench" / ".venv").is_symlink())
            self.assertIn(".venv", proc.stdout, "the removal is logged")
            self.assertIn(STRAY_TARGET, proc.stdout, "the log says what the link pointed at")
            # The rest of the export is as before: the case is there.
            self.assertTrue((out / "bench" / "tasks" / "a-case" / "task.yaml").is_file())

    def test_the_regression_this_guards(self):
        # Without the guard, uv's mkdir over the dangling link is the os
        # error 17 the gate failed with on every pull request.
        with tempfile.TemporaryDirectory() as tmp:
            bench = pathlib.Path(tmp) / "bench"
            bench.mkdir()
            (bench / ".venv").symlink_to(STRAY_TARGET)
            proc = run_bash("\n".join([UV_STUB, f'cd "{bench}" && uv sync --quiet']))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("File exists", proc.stderr)


class GuardTest(unittest.TestCase):
    def run_guard(self, bench: pathlib.Path) -> subprocess.CompletedProcess:
        return run_bash(f'{GUARD} "{bench}"')

    def test_a_real_venv_directory_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            bench = pathlib.Path(tmp) / "bench"
            (bench / ".venv" / "bin").mkdir(parents=True)
            (bench / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
            proc = self.run_guard(bench)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "", "nothing to say about a directory")
            self.assertTrue((bench / ".venv" / "bin" / "python").is_file())

    def test_a_venv_file_is_dropped_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            bench = pathlib.Path(tmp) / "bench"
            bench.mkdir()
            (bench / ".venv").write_text("not a venv\n")
            proc = self.run_guard(bench)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((bench / ".venv").exists())
            self.assertIn(".venv", proc.stdout)

    def test_a_link_to_a_real_directory_is_dropped_as_well(self):
        # Not dangling on this machine, but still not the scratch tree's own
        # venv: uv must build its own, not fill someone else's.
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = pathlib.Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            bench = pathlib.Path(tmp) / "bench"
            bench.mkdir()
            (bench / ".venv").symlink_to(elsewhere)
            proc = self.run_guard(bench)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((bench / ".venv").is_symlink())
            self.assertTrue(elsewhere.is_dir(), "the link is removed, not what it points at")

    def test_a_tree_without_a_venv_is_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            bench = pathlib.Path(tmp) / "bench"
            bench.mkdir()
            proc = self.run_guard(bench)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "")

    def test_the_export_calls_the_guard(self):
        self.assertIn(GUARD, lifted(EXPORT))


if __name__ == "__main__":
    unittest.main()
