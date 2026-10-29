"""collector_run: the one owner of the collectors' subprocess plumbing, and the
check that no collector grows a copy of it back."""

import ast
import functools
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import collector_run as cr  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]
# Every script under a skill's scripts/ tree, nested packages included.
SKILL_SCRIPTS = sorted(
    p for p in REPO.glob("agents/platform/skills/*/scripts/**/*.py") if not p.name.startswith("test_")
)
# The collectors that bind the shared names, with the timeout constant each binds.
COLLECTORS = {
    "collect": "DEFAULT_TIMEOUT_S",
    "fleet_waste": "DEFAULT_TIMEOUT_S",
    "fleet_stockout": "DEFAULT_TIMEOUT_S",
    "fleet_drift": "DEFAULT_TIMEOUT_S",
    "fleet_health": "DEFAULT_TIMEOUT_S",
    "patch_readiness": "DEFAULT_TIMEOUT_S",
    "upgrade_retrospective": "GCLOUD_TIMEOUT_S",
    "compute_fleet_audit": "DEFAULT_TIMEOUT_S",
}
OWNED_NAMES = ("Run", "default_run", "_text", "run_and_gate", "Discovery")
# collect.py keeps its own run_and_gate: it adds the kubeconfig env and the
# `.items` gate, a different contract from the JSON gate here.
LOCAL_EXCEPTIONS = {("collect.py", "run_and_gate")}
TIMED_OUT = subprocess.TimeoutExpired(["gcloud"], 1, output=b"partial", stderr=b"WARNING: zone did not respond")


def load_collector(name):
    for path in SKILL_SCRIPTS:
        if path.stem == name:
            sys.path.insert(0, str(path.parent))
            try:
                return __import__(name)
            finally:
                sys.path.pop(0)
    raise AssertionError(f"no collector named {name}")


def _is_collector_run_attr(node, attr=None):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "collector_run" and (attr is None or node.attr == attr))


def binds_shared(value):
    """True for the two admitted binding shapes: `collector_run.<name>` and
    `functools.partial(collector_run.default_run, ...)`; any other call is a copy."""
    if _is_collector_run_attr(value):
        return True
    if isinstance(value, ast.Call) and value.args and _is_collector_run_attr(value.args[0], "default_run"):
        callee = value.func
        return ((isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)
                 and callee.value.id == "functools" and callee.attr == "partial")
                or (isinstance(callee, ast.Name) and callee.id == "partial"))
    return False


def copies_in(paths, root=REPO):
    """Skill scripts that define an owned name: a `def`, a `class`, or an
    assignment such as `Run = namedtuple(...)` that does not bind the shared one."""
    copies = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = str(path.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in OWNED_NAMES:
                if (path.name, node.name) not in LOCAL_EXCEPTIONS:
                    copies.setdefault(where, []).append(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in OWNED_NAMES and not binds_shared(node.value):
                        copies.setdefault(where, []).append(f"{target.id} = ...")
    return copies


def strays_in(paths, root=REPO):
    """Places that reach collector_run other than a module-level `import collector_run`
    and the module-level binding lines: a direct `collector_run.<name>` use, an aliased
    import, or a `from collector_run import ...` anywhere."""
    stray = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where = str(path.relative_to(root))
        allowed = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                allowed.update(id(n) for n in ast.walk(node) if isinstance(n, ast.Attribute))
            if isinstance(node, ast.Import) and all(a.name == "collector_run" and a.asname is None for a in node.names):
                allowed.add(id(node))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "collector_run" and id(node) not in allowed):
                stray.setdefault(where, []).append(f"{node.lineno}:{node.attr}")
            elif isinstance(node, ast.Import) and id(node) not in allowed and any(a.name == "collector_run" for a in node.names):
                stray.setdefault(where, []).append(f"{node.lineno}:import collector_run outside the module-level plain import")
            elif isinstance(node, ast.ImportFrom) and node.module == "collector_run":
                stray.setdefault(where, []).append(f"{node.lineno}:from collector_run import ...")
    return stray


def bound_timeout_name(mod):
    """The constant the collector's `default_run = functools.partial(...)` line names."""
    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(x, ast.Name) and x.id == "default_run" for x in node.targets):
            for kw in getattr(node.value, "keywords", []):
                if kw.arg == "timeout" and isinstance(kw.value, ast.Name):
                    return kw.value.id
    return None


class DefaultRunTest(unittest.TestCase):
    def test_a_timeout_that_wrote_output_returns_text(self):
        with mock.patch.object(subprocess, "run", side_effect=TIMED_OUT):
            result = cr.default_run(["gcloud"], timeout=1)
        self.assertEqual((result.rc, result.stdout, result.stderr), (cr.TIMEOUT_RC, "partial", "WARNING: zone did not respond"))

    def test_a_timeout_that_wrote_nothing_returns_empty_text(self):
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["gcloud"], 1)):
            result = cr.default_run(["gcloud"], timeout=1)
        self.assertEqual((result.rc, result.stdout, result.stderr), (cr.TIMEOUT_RC, "", ""))

    def test_a_completed_run_passes_its_text_through(self):
        done = subprocess.CompletedProcess(["gcloud"], 3, stdout="[]", stderr="warn")
        with mock.patch.object(subprocess, "run", return_value=done):
            result = cr.default_run(["gcloud"], timeout=5)
        self.assertEqual((result.rc, result.stdout, result.stderr), (3, "[]", "warn"))

    def test_a_missing_binary_is_a_failed_run_not_a_raise(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError(2, "No such file", "gcloud")):
            result = cr.default_run(["gcloud"], timeout=5)
        self.assertEqual((result.rc, result.stdout), (-1, ""))
        self.assertIn("No such file", result.stderr)

    def test_env_and_timeout_reach_the_child(self):
        done = subprocess.CompletedProcess(["gcloud"], 0, stdout="", stderr="")
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            cr.default_run(["gcloud"], env={"KUBECONFIG": "/k"}, timeout=7)
        self.assertEqual(run.call_args.kwargs["env"], {"KUBECONFIG": "/k"})
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_the_timeout_has_no_default(self):
        with self.assertRaises(TypeError):
            cr.default_run(["gcloud"])


class RunAndGateTest(unittest.TestCase):
    def run_of(self, rc, stdout="", stderr=""):
        return cr.Run(["gcloud"], rc, stdout, stderr, 0.0)

    def test_a_failed_command_gates_closed(self):
        parsed, result = cr.run_and_gate(["gcloud"], run=lambda argv, **_: self.run_of(1, "[]"))
        self.assertEqual((parsed, result.rc), (None, 1))

    def test_empty_and_non_json_output_gate_closed(self):
        for stdout in ("", "   ", "not json"):
            parsed, _ = cr.run_and_gate(["gcloud"], run=lambda argv, **_: self.run_of(0, stdout))
            self.assertIsNone(parsed, stdout)

    def test_json_output_is_parsed(self):
        parsed, _ = cr.run_and_gate(["gcloud"], run=lambda argv, **_: self.run_of(0, '{"a": [1]}'))
        self.assertEqual(parsed, {"a": [1]})

    def test_env_is_passed_only_when_given(self):
        calls = []

        def run(argv, **kw):
            calls.append(kw)
            return self.run_of(0, "[]")

        cr.run_and_gate(["gcloud"], run=run)
        cr.run_and_gate(["gcloud"], run=run, env={"A": "1"})
        self.assertEqual(calls, [{}, {"env": {"A": "1"}}])


class CollectorsBindTheSharedNamesTest(unittest.TestCase):
    """Each collector addresses the shared plumbing through its own module name,
    so its tests and fakes keep working, and binds its own timeout."""

    def test_every_collector_binds_run_and_default_run(self):
        for name, timeout_const in COLLECTORS.items():
            with self.subTest(collector=name):
                mod = load_collector(name)
                self.assertIs(mod.Run, cr.Run)
                self.assertIsInstance(mod.default_run, functools.partial)
                self.assertIs(mod.default_run.func, cr.default_run)
                self.assertEqual(mod.default_run.keywords, {"timeout": getattr(mod, timeout_const)})
                self.assertEqual(bound_timeout_name(mod), timeout_const)
                if hasattr(mod, "Discovery"):
                    self.assertIs(mod.Discovery, cr.Discovery)
                if name != "collect" and hasattr(mod, "run_and_gate"):
                    self.assertIs(mod.run_and_gate, cr.run_and_gate)

    def test_no_skill_script_defines_a_copy_of_the_shared_names(self):
        self.assertTrue(any(p.parent.name != "scripts" for p in SKILL_SCRIPTS), "the scan does not reach nested packages")
        self.assertEqual({}, copies_in(SKILL_SCRIPTS), "these skill scripts grew a copy of a name collector_run owns")

    def test_collectors_reach_the_shared_names_only_through_their_bindings(self):
        """A body that reaches the module directly skips the collector's binding, so
        a test that patches `<collector>.default_run` would run the real subprocess
        and still pass."""
        self.assertEqual({}, strays_in(SKILL_SCRIPTS), "collector_run reached outside the module-level import and bindings")

    def test_the_guards_refuse_the_shapes_they_exist_for(self):
        """One planted file per hole: a copy in a nested package, a namedtuple copy,
        a wrapped binding that is not `functools.partial`, a from-import alias
        inside a function, and an aliased module import."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            nested = root / "skill" / "scripts" / "rules"
            nested.mkdir(parents=True)
            (nested / "deep.py").write_text("def default_run(argv, *, timeout=1):\n    return None\n")
            (root / "skill" / "scripts" / "tuple.py").write_text(
                "import collections\nRun = collections.namedtuple('Run', 'argv rc stdout stderr duration_s')\n")
            (root / "skill" / "scripts" / "fromimp.py").write_text(
                "def go(argv):\n    from collector_run import default_run as _r\n    return _r(argv, timeout=1)\n")
            (root / "skill" / "scripts" / "alias.py").write_text(
                "import collector_run as cr\n\n\ndef go(argv):\n    return cr.default_run(argv, timeout=1)\n")
            (root / "skill" / "scripts" / "wrapped.py").write_text(
                "import collector_run\ndefault_run = retrying(collector_run.default_run)\n")
            (root / "skill" / "scripts" / "fine.py").write_text(
                "import collector_run\nRun = collector_run.Run\n"
                "default_run = functools.partial(collector_run.default_run, timeout=5)\n")
            paths = sorted(root.glob("skill/scripts/**/*.py"))
            self.assertEqual(
                {"skill/scripts/rules/deep.py": ["default_run"], "skill/scripts/tuple.py": ["Run = ..."],
                 "skill/scripts/wrapped.py": ["default_run = ..."]},
                copies_in(paths, root=root))
            self.assertEqual(
                {"skill/scripts/alias.py": ["1:import collector_run outside the module-level plain import"],
                 "skill/scripts/fromimp.py": ["2:from collector_run import ..."]},
                strays_in(paths, root=root))


if __name__ == "__main__":
    unittest.main()
