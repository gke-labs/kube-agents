#!/usr/bin/env python3
"""No shipped script puts a worker's override tree on its import path (#3551).

In #3444 an audit worker copied the fleet-audit skill and the shared scripts
into ``skills_override/`` and ``scripts_override/`` in its profile home, patched
them, and edited the copied ``audit_report.py`` to put its own scripts first on
``sys.path``.  Nothing the product ships reads those trees, so a copy only runs
when the worker runs it.  The nightly ``kanban-workspace-gc`` removes them.
This test keeps the first half true, for what it can read: no literal path
given to a shipped import-path call (``sys.path`` insert, append, extend or
slice assignment, ``site.addsitedir``, ``spec_from_file_location``) names an
``_override`` tree or sits under ``/opt/data/profiles/``; no shipped shell
script puts one on ``PYTHONPATH``; and no shipped config names an override
tree, so none can become a skill directory.  A path built from a variable or
the environment is not seen.

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


def _is_sys_path(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id == "sys")


# Calls that put a directory on the import path, or load a module from a file:
# `sys.path.insert/append/extend`, `site.addsitedir`, and
# `importlib.util.spec_from_file_location` (also by its bare name).
def _is_import_path_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("insert", "append", "extend") and _is_sys_path(func.value):
            return True
        return func.attr in ("addsitedir", "spec_from_file_location")
    return isinstance(func, ast.Name) and func.id in ("addsitedir", "spec_from_file_location")


def import_path_strings(source: str) -> tuple[int, list[str]]:
    """How many import-path calls and assignments *source* makes, and every string literal in them.

    Covers `_is_import_path_call` and an assignment to `sys.path` or a slice of
    it.  Only literals count: a path held in a variable set elsewhere, or read
    from the environment, is not seen.  A file Python can't parse (a template,
    a fixture) counts as none.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, []
    count, strings = 0, []
    for node in ast.walk(tree):
        values: list[ast.AST] = []
        if isinstance(node, ast.Call) and _is_import_path_call(node):
            values = [*node.args, *(k.value for k in node.keywords)]
        elif isinstance(node, ast.Assign) and any(
                _is_sys_path(t) or (isinstance(t, ast.Subscript) and _is_sys_path(t.value))
                for t in node.targets):
            values = [node.value]
        else:
            continue
        count += 1
        for value in values:
            strings.extend(n.value for n in ast.walk(value)
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
        # Not vacuous: the shipped tree makes dozens of these calls (98 when this
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

    def test_the_walk_catches_the_other_forms(self):
        for source in (
            'import sys\nsys.path[:0] = ["/opt/data/profiles/platform/scripts_override"]\n',
            'import site\nsite.addsitedir("/opt/data/profiles/platform/scripts_override")\n',
            'import importlib.util as u\nu.spec_from_file_location("m", "/x/skills_override/m.py")\n',
            'from importlib.util import spec_from_file_location\nspec_from_file_location("m", f"{H}/scripts_override/m.py")\n',
        ):
            count, strings = import_path_strings(source)
            self.assertEqual(count, 1, source)
            self.assertTrue([s for s in strings if FORBIDDEN.search(s)], source)

    def test_no_shipped_config_names_an_override_tree(self):
        # Hermes finds skills under the profile's skills/ and any skills.external_dirs
        # a config lists; no shipped config may point one at an override tree.
        offenders = []
        for suffix in (".yaml", ".yml", ".json"):
            for path in shipped(suffix):
                text = path.read_text(encoding="utf-8", errors="replace")
                offenders.extend(f"{path.relative_to(REPO)}: {m.group(0)}"
                                 for m in re.finditer(r"[\w./-]*_override\b[\w./-]*", text))
        self.assertEqual(offenders, [])

    def test_the_walk_catches_a_built_path(self):
        source = 'import os, sys\nsys.path.insert(0, os.path.join(os.environ["HERMES_HOME"], "scripts_override"))\n'
        count, strings = import_path_strings(source)
        self.assertEqual(count, 1)
        self.assertEqual([s for s in strings if FORBIDDEN.search(s)], ["scripts_override"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
