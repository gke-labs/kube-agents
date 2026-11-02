"""Unit tests for apply_kanban_aimd.py against a miniature terminal_tool_result.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The real file is checked in the shipped image by verify_kanban_aimd.py; these
cover the applier's contract (one insert inside ``finalize_foreground_result``,
ahead of the result dict, and a refusal when the anchor is missing, repeated,
outside the function or already applied), that the inserted code reads only
names the function binds, and that the two other patches editing the same
function stay disjoint from this one.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

import apply_kanban_aimd as applier
import apply_kanban_verified_claims as verified_claims
import apply_ssh_shared_master as ssh_shared_master
import patchlib

HERE = Path(__file__).resolve().parent
DOCKERFILE = HERE.parent / "Dockerfile"

# The shape of tools/terminal_tool_result.py at v2026.9.14, cut to what the
# three patches on it touch.
RESULT_STUB = '''import json
from contextlib import contextmanager


@contextmanager
def _quiet(label: str):
    try:
        yield
    except Exception:
        pass


def finalize_foreground_result(
    *, command, result, env, env_type, effective_task_id,
    task_id, session_id, session_key, workdir, command_cwd, approval_note,
):
    """Turn a raw ``env.execute`` result into the tool's JSON result string."""
    output = result.get("output", "")
    returncode = result.get("returncode", 0)
    exit_note = None
    failure_hint = _failure_hint(command, returncode, output, exit_note)
    result_dict = {"output": output, "exit_code": returncode, "error": None}
    optional_fields = [("hint", failure_hint or None)]
    for key, value in optional_fields:
        if value is not None:
            result_dict[key] = value
    return json.dumps(result_dict, ensure_ascii=False)


def _failure_hint(command, returncode, output, exit_note):
    return None
'''


def _tree(source=RESULT_STUB):
    root = Path(tempfile.mkdtemp()) / "hermes"
    target = root / applier.RESULT_RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    return root, target


def _function(source, name):
    return next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


class ApplyTest(unittest.TestCase):
    def test_the_stub_is_a_faithful_stand_in(self):
        ast.parse(RESULT_STUB)
        self.assertEqual(RESULT_STUB.count(applier.RESULT_ANCHOR), 1)
        self.assertEqual(RESULT_STUB.count(ssh_shared_master.RESULT_ANCHOR), 1)
        self.assertEqual(RESULT_STUB.count(verified_claims.OLD_TERMINAL_RETURN), 1)

    def test_the_call_lands_inside_the_function_ahead_of_the_result_dict(self):
        root, target = _tree()
        applier.apply(root)
        patched = target.read_text()
        self.assertIn(applier.RESULT_PATCHED, patched)
        fn = _function(patched, applier.RESULT_HANDLER)
        statements = [ast.unparse(stmt) for stmt in fn.body]
        record = next(i for i, text in enumerate(statements) if "record_worker_pressure" in text)
        build = next(i for i, text in enumerate(statements) if text.startswith("result_dict = "))
        self.assertEqual(record + 1, build)
        self.assertEqual(patched.count("record_worker_pressure("), 1)

    def test_the_inserted_code_reads_only_bound_names(self):
        root, target = _tree()
        applier.apply(root)
        tree = ast.parse(target.read_text())
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == applier.RESULT_HANDLER)
        inserted = next(
            stmt for stmt in fn.body
            if isinstance(stmt, ast.With) and "kanban pressure record" in ast.unparse(stmt)
        )
        # The call, whose names the import ahead of it in the same block binds.
        call = inserted.body[-1]
        self.assertIn("record_worker_pressure", ast.unparse(call))
        self.assertEqual(patchlib.unbound(tree, call), [])

    def test_a_missing_anchor_fails_the_build(self):
        root, _ = _tree(RESULT_STUB.replace(applier.RESULT_ANCHOR, "    result_dict = {}\n"))
        with self.assertRaises(SystemExit):
            applier.apply(root)

    def test_a_duplicated_anchor_fails_the_build(self):
        root, target = _tree(RESULT_STUB + "\n\ndef other():\n" + applier.RESULT_ANCHOR)
        with self.assertRaises(SystemExit):
            applier.apply(root)
        self.assertEqual(target.read_text().count("record_worker_pressure"), 0)

    def test_an_anchor_outside_the_function_fails_the_build(self):
        moved = RESULT_STUB.replace(applier.RESULT_ANCHOR, "    result_dict = {}\n")
        moved += "\n\ndef elsewhere(output, returncode):\n" + applier.RESULT_ANCHOR
        root, _ = _tree(moved)
        with self.assertRaises(SystemExit):
            applier.apply(root)

    def test_a_missing_function_fails_the_build(self):
        root, _ = _tree(RESULT_STUB.replace("def finalize_foreground_result(", "def finalize_result("))
        with self.assertRaises(SystemExit):
            applier.apply(root)

    def test_a_second_run_is_refused_without_writing(self):
        root, target = _tree()
        applier.apply(root)
        once = target.read_text()
        with self.assertRaises(SystemExit):
            applier.apply(root)
        self.assertEqual(target.read_text(), once)

    def test_a_missing_file_fails_the_build(self):
        with self.assertRaises(SystemExit):
            applier.apply(Path(tempfile.mkdtemp()))


class NeighbourTest(unittest.TestCase):
    """verified_claims runs before this applier and ssh_shared_master after, on the same function."""

    def test_the_three_anchors_are_disjoint(self):
        for name, other in (
            ("ssh_shared_master", ssh_shared_master.RESULT_ANCHOR),
            ("verified_claims", verified_claims.OLD_TERMINAL_RETURN),
        ):
            with self.subTest(neighbour=name):
                self.assertNotIn(other, applier.RESULT_ANCHOR)
                self.assertNotIn(applier.RESULT_ANCHOR, other)
                self.assertNotIn(other.rstrip("\n"), applier.RESULT_PATCHED.replace(applier.RESULT_ANCHOR, ""))

    def test_the_neighbours_still_apply_around_this_one_in_build_order(self):
        root, target = _tree()
        verified_claims_patch = patchlib.Patch(root, applier.RESULT_RELATIVE, prefix="t")
        verified_claims_patch.substitute(
            verified_claims.OLD_TERMINAL_RETURN, verified_claims.NEW_TERMINAL_RETURN, label="terminal return",
        )
        verified_claims_patch.commit("verified claims")
        applier.apply(root)
        ssh_patch = patchlib.Patch(root, applier.RESULT_RELATIVE, prefix="t")
        ssh_patch.refuse_if_patched(ssh_shared_master.MARKER)
        ssh_patch.substitute(ssh_shared_master.RESULT_ANCHOR, ssh_shared_master.RESULT_PATCHED, label="hint")
        ssh_patch.commit("ssh shared master")
        patched = target.read_text()
        fn = _function(patched, applier.RESULT_HANDLER)
        texts = [ast.unparse(stmt) for stmt in fn.body]
        hint = next(i for i, t in enumerate(texts) if "ssh_shared_master" in t or "Exit 255 with no exit marker" in t)
        record = next(i for i, t in enumerate(texts) if "record_worker_pressure" in t)
        claims = next(i for i, t in enumerate(texts) if "record_terminal_call" in t)
        self.assertLess(hint, record)
        self.assertLess(record, claims)


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        if not DOCKERFILE.exists():
            self.skipTest("Dockerfile not beside the patches")
        self.text = DOCKERFILE.read_text()

    def test_the_gate_greps_for_what_the_applier_writes(self):
        self.assertIn(applier.BUILD_MARKER, self.text)

    def test_the_three_files_are_copied_into_the_scheduling_stage(self):
        for name in ("kanban_aimd.py", "apply_kanban_aimd.py", "verify_kanban_aimd.py"):
            with self.subTest(file=name):
                self.assertIn(f"deploy/docker/patches/{name} \\", self.text)

    def test_the_runtime_is_installed_before_either_applier_runs(self):
        install = self.text.index(
            "install -m 0644 /tmp/kanban-scheduling/kanban_aimd.py /opt/hermes/hermes_cli/kanban_aimd.py"
        )
        scheduling = self.text.index("/tmp/kanban-scheduling/apply_kanban_scheduling.py /opt/hermes")
        aimd = self.text.index("/tmp/kanban-scheduling/apply_kanban_aimd.py /opt/hermes")
        verify = self.text.index("/tmp/kanban-scheduling/verify_kanban_aimd.py")
        self.assertLess(install, scheduling)
        self.assertLess(install, aimd)
        self.assertLess(aimd, verify)

    def test_the_applier_runs_between_its_two_neighbours(self):
        aimd = self.text.index("/tmp/kanban-scheduling/apply_kanban_aimd.py /opt/hermes")
        self.assertLess(self.text.index("apply_kanban_verified_claims.py /opt/hermes"), aimd)
        self.assertLess(aimd, self.text.index("apply_ssh_shared_master.py /opt/hermes"))


if __name__ == "__main__":
    unittest.main()
