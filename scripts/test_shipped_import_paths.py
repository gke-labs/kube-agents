#!/usr/bin/env python3
"""No shipped script puts a worker's override tree on its import path (#3551).

In #3444 an audit worker copied the fleet-audit skill and the shared scripts
into ``skills_override/`` and ``scripts_override/`` in its profile home, patched
them, and edited the copied ``audit_report.py`` to put its own scripts first on
``sys.path``.  Nothing the product ships reads those trees, so a copy only runs
when the worker runs it.  The nightly ``kanban-workspace-gc`` removes them.
This test keeps the first half true: no shipped Python file adds a path that
names an ``_override`` tree or sits inside a profile home, and no shipped shell
script sets one on ``PYTHONPATH``.

Run: python3 scripts/test_shipped_import_paths.py
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHIPPED_ROOTS = ("agents", "deploy")
# A path a shipped script must never import from: a worker's override copy,
# or anything under a profile home, which is the worker's to write.
FORBIDDEN = re.compile(r"_override\b|/opt/data/profiles/")
PYTHONPATH_LINE = re.compile(r"PYTHONPATH=\S*")


def shipped(suffix: str) -> list[Path]:
    files = []
    for root in SHIPPED_ROOTS:
        for path in sorted((REPO / root).rglob(f"*{suffix}")):
            if path.name.startswith("test_") or "/tests/" in path.as_posix():
                continue
            files.append(path)
    return files


def _is_sys_path_mutation(node: ast.Call) -> bool:
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr in ("insert", "append", "extend")
            and isinstance(func.value, ast.Attribute) and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name) and func.value.value.id == "sys")


def import_path_strings(source: str) -> tuple[int, list[str]]:
    """How many sys.path mutations *source* makes, and every string literal in their arguments.

    A file Python can't parse (a template, a fixture) counts as none.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, []
    count, strings = 0, []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_sys_path_mutation(node):
            count += 1
            for arg in node.args:
                strings.extend(n.value for n in ast.walk(arg)
                               if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return count, strings


class ShippedImportPathsTest(unittest.TestCase):

    def test_no_shipped_python_imports_from_an_override_or_a_profile_home(self):
        total, offenders = 0, []
        for path in shipped(".py"):
            count, strings = import_path_strings(path.read_text(encoding="utf-8", errors="replace"))
            total += count
            offenders.extend(f"{path.relative_to(REPO)}: {s!r}" for s in strings if FORBIDDEN.search(s))
        self.assertEqual(offenders, [])
        # Not vacuous: the shipped tree makes dozens of these calls (82 when this
        # was written).  A walk that found almost none has stopped reading the tree.
        self.assertGreater(total, 40)

    def test_no_shipped_shell_script_puts_one_on_pythonpath(self):
        offenders = []
        for path in shipped(".sh"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for match in PYTHONPATH_LINE.finditer(line):
                    if FORBIDDEN.search(match.group(0)):
                        offenders.append(f"{path.relative_to(REPO)}:{lineno}: {match.group(0)}")
        self.assertEqual(offenders, [])

    def test_the_walk_catches_the_line_from_3444(self):
        line = 'sys.path.insert(0, "/opt/data/profiles/platform/scripts_override"); sys.path.append("/opt/defaults/scripts")\n'
        count, strings = import_path_strings("import sys\n" + line)
        self.assertEqual(count, 2)
        self.assertEqual([s for s in strings if FORBIDDEN.search(s)],
                         ["/opt/data/profiles/platform/scripts_override"])

    def test_the_walk_catches_a_built_path(self):
        source = 'import os, sys\nsys.path.insert(0, os.path.join(os.environ["HERMES_HOME"], "scripts_override"))\n'
        count, strings = import_path_strings(source)
        self.assertEqual(count, 1)
        self.assertEqual([s for s in strings if FORBIDDEN.search(s)], ["scripts_override"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
