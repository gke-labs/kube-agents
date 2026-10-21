#!/usr/bin/env python3
"""No literal path in a shipped import-path call names a worker's override tree (#3551).

In #3444 an audit worker copied the fleet-audit skill and the shared scripts
into ``skills_override/`` and ``scripts_override/`` in its profile home, patched
them, and edited the copied ``audit_report.py`` to put its own scripts first on
``sys.path``.  Nothing the product ships reads those trees, so a copy only runs
when the worker runs it.  The nightly ``kanban-workspace-gc`` removes them.
This test keeps the first half true, for what it can read: no literal path
given to a shipped import-path call or assignment (``sys.path`` insert,
append, extend, slice or ``+=``, however ``sys`` was imported;
``site.addsitedir``, ``spec_from_file_location``, ``SourceFileLoader``,
``runpy.run_path``; ``os.environ["PYTHONPATH"]`` and ``os.putenv``) names an
``_override`` tree or sits under ``/opt/data/profiles``; no shipped shell
script puts one on ``PYTHONPATH``; and no shipped config names an override
tree, so none can become a skill directory.  Not seen: a path built from a
variable or the environment, a ``PYTHONPATH`` passed to a child in an ``env=``
dict or ``environ.update``, the Dockerfile and operator ``PYTHONPATH``, and
``.pth`` files.  It is a guard on the obvious forms, not a proof; the nightly
sweep is what removes the copies.

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
FORBIDDEN = re.compile(r"_override\b|/opt/data/profiles\b")
PYTHONPATH_LINE = re.compile(r"PYTHONPATH\+?=\S*")
# An override tree as a path in a config (a skill directory, say), not a key
# that merely ends in the suffix.
CONFIG_OVERRIDE_PATH = re.compile(r"[\w./-]*(?:/[\w.-]*_override\b|_override/)[\w./-]*")


def shipped(suffix: str) -> list[Path]:
    files = []
    for root in SHIPPED_ROOTS:
        for path in sorted((REPO / root).rglob(f"*{suffix}")):
            if path.name.startswith("test_") or "/tests/" in path.as_posix():
                continue
            files.append(path)
    return files


def _aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to the sys module, and names bound to sys.path, by an import in *tree*."""
    sys_names, path_names = {"sys"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            sys_names.update(a.asname or a.name for a in node.names if a.name == "sys")
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            path_names.update(a.asname or a.name for a in node.names if a.name == "path")
    return sys_names, path_names


def _is_sys_path(node: ast.AST, sys_names: set[str], path_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in path_names
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id in sys_names)


# Calls that put a directory on the import path or run Python from a file:
# `sys.path.insert/append/extend` (however sys or its path was imported),
# `site.addsitedir`, `importlib.util.spec_from_file_location`,
# `importlib.machinery.SourceFileLoader`, `runpy.run_path`, and `os.putenv`
# (for PYTHONPATH).  Matched by the called name, so an alias of the module
# doesn't hide one.
FILE_LOADERS = ("addsitedir", "spec_from_file_location", "SourceFileLoader", "run_path", "putenv")


def _is_import_path_call(node: ast.Call, sys_names: set[str], path_names: set[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("insert", "append", "extend") and _is_sys_path(func.value, sys_names, path_names):
            return True
        return func.attr in FILE_LOADERS
    return isinstance(func, ast.Name) and func.id in FILE_LOADERS


def _is_pythonpath_target(node: ast.AST) -> bool:
    """`os.environ["PYTHONPATH"]` (or any `environ[...]` subscript keyed by that literal)."""
    return (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and node.slice.value == "PYTHONPATH")


def import_path_strings(source: str) -> tuple[int, list[str]]:
    """How many import-path calls and assignments *source* makes, and every string literal in them.

    Covers `_is_import_path_call`, an assignment or augmented assignment to
    `sys.path` or a slice of it, and an assignment to `os.environ["PYTHONPATH"]`.
    Only literals count: a path held in a variable set elsewhere, or read from
    the environment, is not seen.  A file Python can't parse (a template, a
    fixture) counts as none.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, []
    sys_names, path_names = _aliases(tree)

    def is_target(t: ast.AST) -> bool:
        return (_is_sys_path(t, sys_names, path_names) or _is_pythonpath_target(t)
                or (isinstance(t, ast.Subscript) and _is_sys_path(t.value, sys_names, path_names)))

    count, strings = 0, []
    for node in ast.walk(tree):
        values: list[ast.AST] = []
        if isinstance(node, ast.Call) and _is_import_path_call(node, sys_names, path_names):
            values = [*node.args, *(k.value for k in node.keywords)]
        elif isinstance(node, ast.Assign) and any(is_target(t) for t in node.targets):
            values = [node.value]
        elif isinstance(node, ast.AugAssign) and is_target(node.target):
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

    def test_the_walk_catches_aliases_and_more_loaders(self):
        for source in (
            'import sys\nsys.path += ["/opt/data/profiles/platform/scripts_override"]\n',
            'import sys as _s\n_s.path.insert(0, "/opt/data/profiles/platform/scripts")\n',
            'from sys import path\npath.insert(0, "/x/scripts_override")\n',
            'import runpy\nrunpy.run_path("/opt/data/profiles/platform/scripts_override/audit_report.py")\n',
            'from importlib.machinery import SourceFileLoader\nSourceFileLoader("m", "/x/skills_override/m.py")\n',
            'import os\nos.environ["PYTHONPATH"] = "/opt/data/profiles/platform/scripts_override"\n',
            'import os\nos.path.join("/opt/data/profiles", "platform", "scripts")\nimport sys\nsys.path.insert(0, os.path.join("/opt/data/profiles", "platform", "scripts"))\n',
        ):
            count, strings = import_path_strings(source)
            self.assertGreaterEqual(count, 1, source)
            self.assertTrue([s for s in strings if FORBIDDEN.search(s)], source)

    def test_the_config_check_wants_a_path_not_a_key(self):
        self.assertIsNone(CONFIG_OVERRIDE_PATH.search("image_override: true"))
        self.assertTrue(CONFIG_OVERRIDE_PATH.search("external_dirs: [/opt/data/profiles/platform/skills_override]"))
        self.assertTrue(CONFIG_OVERRIDE_PATH.search("dir: skills_override/fleet-audit"))

    def test_pythonpath_append_in_shell_is_read(self):
        self.assertTrue(FORBIDDEN.search(PYTHONPATH_LINE.search("PYTHONPATH+=:/opt/data/profiles/platform/x").group(0)))

    def test_no_shipped_config_names_an_override_tree(self):
        # Hermes finds skills under the profile's skills/ and any skills.external_dirs
        # a config lists; no shipped config may point one at an override tree.
        offenders = []
        for suffix in (".yaml", ".yml", ".json"):
            for path in shipped(suffix):
                text = path.read_text(encoding="utf-8", errors="replace")
                offenders.extend(f"{path.relative_to(REPO)}: {m.group(0)}"
                                 for m in CONFIG_OVERRIDE_PATH.finditer(text))
        self.assertEqual(offenders, [])

    def test_the_walk_catches_a_built_path(self):
        source = 'import os, sys\nsys.path.insert(0, os.path.join(os.environ["HERMES_HOME"], "scripts_override"))\n'
        count, strings = import_path_strings(source)
        self.assertEqual(count, 1)
        self.assertEqual([s for s in strings if FORBIDDEN.search(s)], ["scripts_override"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
