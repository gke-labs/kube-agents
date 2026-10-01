"""Unit tests for skill_manage_image_owned.py, its applier, and its build-time verifier."""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import apply_skill_manage_image_owned
import skill_manage_image_owned as gate

# The v2026.9.14 shape of tools/skill_manager_tool.py::skill_manage, reduced
# to what parses and runs on its own. Byte-identical to upstream for the
# anchored lines: the ``operations`` dispatch and the preflight walrus. The
# handlers below stand in for upstream's: a call that gets past the gate is
# recorded, which is what the verifier needs to tell "not refused" from
# "refused".
UPSTREAM = '''\
import json

CALLS = []


def _skill_manage_batch(operations, default_name=None, task_id=None, session_id=None):
    # Upstream re-enters skill_manage once per operation (_skill_manage_from);
    # the stub does the same so a gate placed inside skill_manage is exercised
    # by the batch shape too.
    results = []
    for op in operations:
        raw = skill_manage(action=op.get("action", ""), name=op.get("name") or default_name or "",
                           content=op.get("content"), category=op.get("category"),
                           old_string=op.get("old_string"), new_string=op.get("new_string"),
                           operations=None)
        results.append(json.loads(raw))
    return json.dumps({"success": all(r.get("success") for r in results), "results": results})


def _background_review_preflight(action, name):
    return None


def skill_manage(
    action: str, name: str, content: str = None, category: str = None, file_path: str = None,
    file_content: str = None, old_string: str = None, new_string: str = None,
    replace_all: bool = False, absorbed_into: str = None, task_id: str = None,
    session_id: str = None, operations=None) -> str:
    """Dispatch to the action handler -> JSON string."""
    if operations is not None:
        return _skill_manage_batch(
            operations, default_name=name or None, task_id=task_id, session_id=session_id)
    if (preflight := _background_review_preflight(action, name)) is not None:
        return json.dumps(preflight, ensure_ascii=False)
    CALLS.append((action, name))
    if action not in {"create", "edit", "patch", "delete", "write_file", "remove_file"}:
        return json.dumps({"success": False, "error": f"Unknown action '{action}'"})
    return json.dumps({"success": True, "action": action, "name": name})


from tools.registry import registry, tool_error
'''

# The v2026.9.14 shape of agent/file_safety.py's classification and message,
# reduced to what runs on its own. Byte-identical to upstream for the two
# anchored spans: the safe-root tail of _classify_write_denial and the last
# line of get_write_denied_error. _hermes_dirs reads HERMES_HOME the way the
# verifier drives it.
SAFETY_UPSTREAM = '''\
import os
from pathlib import Path


def _hermes_dirs():
    home = os.environ.get("HERMES_HOME", "")
    return [Path(home)] if home else []


def _is_under(resolved, base):
    return resolved == base or resolved.startswith(str(base) + os.sep)


def _home_and_resolved(path):
    return tuple(os.path.realpath(os.path.expanduser(p)) for p in ("~", str(path)))


def get_safe_write_roots():
    return {os.path.realpath(p) for p in filter(None, os.getenv("HERMES_WRITE_SAFE_ROOT", "").split(os.pathsep))}


def build_write_approval_paths(home):
    return set()


def build_write_denied_paths(home):
    return set()


def build_write_denied_prefixes(home):
    return []


def _classify_write_denial(path):
    home, resolved = _home_and_resolved(path)
    if resolved in build_write_approval_paths(home):
        return None
    if resolved in build_write_denied_paths(home) or any(
        resolved.startswith(prefix) for prefix in build_write_denied_prefixes(home)
    ):
        return "credential"
    safe_roots = get_safe_write_roots()
    if safe_roots and not any(_is_under(resolved, root) for root in safe_roots):
        return "safe_root"

    return None


def get_write_denied_error(path, *, verb="Write"):
    denial = _classify_write_denial(path)
    if denial == "safe_root":
        return f"{verb} denied: '{path}' is outside HERMES_WRITE_SAFE_ROOT."
    return f"{verb} denied: '{path}' is a protected system/credential file." if denial else None
'''

REGISTRY_STUB = '''\
import json

registry = None


def tool_error(message, success=False):
    return json.dumps({"success": success, "error": message})
'''


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.platform_root = self.tmp / "platform-template" / "skills"
        self.cluster_root = self.tmp / "cluster-template" / "skills"
        for root, names in ((self.platform_root, ("submit-suggestion", "gke-basics")),
                            (self.cluster_root, ("gke-stall-detection", "gke-basics"))):
            for name in names:
                (root / name).mkdir(parents=True)
                (root / name / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")
        self.patched = mock.patch.multiple(
            gate, PLATFORM_ROOTS=(str(self.platform_root),), CLUSTER_ROOTS=(str(self.cluster_root),))
        self.patched.start()
        self.addCleanup(self.patched.stop)

    def test_the_profile_is_read_from_where_the_home_sits(self):
        self.assertEqual(gate.profile_for_home(pathlib.Path("/opt/data/profiles/platform")), "platform")
        self.assertEqual(gate.profile_for_home(pathlib.Path("/opt/data/profiles/cluster-a")), "cluster-a")
        self.assertEqual(gate.profile_for_home(pathlib.Path("/opt/data")), "default")
        self.assertEqual(gate.profile_for_home(pathlib.Path("/home/agent/.hermes")), "default")

    def test_shipped_is_answered_per_profile_from_the_template_tree(self):
        self.assertEqual(gate.image_shipped_skill("submit-suggestion", "platform"), str(self.platform_root))
        self.assertIsNone(gate.image_shipped_skill("submit-suggestion", "cluster-a"))
        self.assertEqual(gate.image_shipped_skill("gke-stall-detection", "cluster-a"), str(self.cluster_root))
        self.assertIsNone(gate.image_shipped_skill("gke-stall-detection", "platform"))
        # Present in both templates: shipped for both, each from its own tree.
        self.assertEqual(gate.image_shipped_skill("gke-basics", "platform"), str(self.platform_root))
        self.assertEqual(gate.image_shipped_skill("gke-basics", "cluster-a"), str(self.cluster_root))
        self.assertIsNone(gate.image_shipped_skill("submit-suggestion", "default"))
        self.assertIsNone(gate.image_shipped_skill("zz-local-note", "platform"))

    def test_the_last_path_component_is_what_is_compared(self):
        # A category-nested skill under a shipped name would shadow the shipped
        # one in a lookup by bare name, so it is refused too.
        # `gke-basics/..` is nonsense upstream rejects anyway; here it counts as
        # touching gke-basics, the safe direction for a guard.
        for spelling in ("ops/gke-basics", "ops\\gke-basics", "gke-basics/", " gke-basics ", "gke-basics/.."):
            with self.subTest(spelling=spelling):
                self.assertIsNotNone(gate.image_shipped_skill(spelling, "platform"))
        for spelling in ("", ".", "..", "/"):
            with self.subTest(spelling=spelling):
                self.assertIsNone(gate.image_shipped_skill(spelling, "platform"))

    def test_the_refusal_covers_every_write_action_and_nothing_else(self):
        home = pathlib.Path("/opt/data/profiles/platform")
        none = lambda _n: None  # noqa: E731 - no Hermes lookup in these tests
        for action in sorted(gate.WRITE_ACTIONS):
            with self.subTest(action=action):
                message = gate.image_owned_refusal(action, "submit-suggestion", hermes_home=home, locate=none)
                self.assertIsNotNone(message)
                self.assertIn("image-owned", message)
                self.assertIn("platform profile", message)
                self.assertIn("say so in your result", message)
        self.assertIsNone(gate.image_owned_refusal("frobnicate", "submit-suggestion", hermes_home=home, locate=none))
        self.assertIsNone(gate.image_owned_refusal("patch", "zz-local-note", hermes_home=home, locate=none))
        self.assertIsNone(gate.image_owned_refusal("patch", "", hermes_home=home, locate=none))
        self.assertIsNone(gate.image_owned_refusal(
            "patch", "submit-suggestion", hermes_home=pathlib.Path("/opt/data"), locate=none))

    def test_a_category_or_a_nested_home_that_names_a_shipped_skill_is_refused(self):
        home = pathlib.Path("/opt/data/profiles/platform")
        none = lambda _n: None  # noqa: E731
        self.assertIn("'gke-basics'", gate.image_owned_refusal(
            "create", "scripts", category="gke-basics", hermes_home=home, locate=none) or "")
        self.assertIn("'gke-basics'", gate.image_owned_refusal(
            "patch", "gke-basics/scripts", hermes_home=home, locate=none) or "")
        nested = pathlib.Path("/opt/data/profiles/platform/skills/gke-basics/scripts")
        self.assertIn("'gke-basics'", gate.image_owned_refusal(
            "delete", "scripts", hermes_home=home, locate=lambda _n: nested,
            skills_root=lambda: pathlib.Path("/opt/data/profiles/platform/skills")) or "")
        self.assertIsNone(gate.image_owned_refusal(
            "create", "scripts", category="my-own-area", hermes_home=home, locate=none))

    def test_a_batch_is_refused_whole_when_any_operation_touches_a_shipped_skill(self):
        home = pathlib.Path("/opt/data/profiles/platform")
        none = lambda _n: None  # noqa: E731
        ops = [{"action": "create", "name": "zz-local-note", "content": "x"},
               {"action": "patch", "name": "submit-suggestion", "old_string": "a", "new_string": "b"}]
        message = gate.image_owned_batch_refusal(ops, None, hermes_home=home, locate=none)
        self.assertIsNotNone(message)
        self.assertIn("submit-suggestion", message)
        # default_name fills an operation that names no skill of its own.
        self.assertIsNotNone(gate.image_owned_batch_refusal(
            [{"action": "delete"}], "gke-basics", hermes_home=home, locate=none))
        self.assertIsNone(gate.image_owned_batch_refusal(
            [{"action": "create", "name": "zz-local-note", "content": "x"}], None, hermes_home=home, locate=none))
        self.assertIsNone(gate.image_owned_batch_refusal("not a list", None, hermes_home=home, locate=none))

    def test_the_home_falls_back_to_the_environment_without_hermes(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/opt/data/profiles/cluster-b"}):
            self.assertIsNotNone(gate.image_owned_refusal("delete", "gke-stall-detection"))
            self.assertIsNone(gate.image_owned_refusal("delete", "submit-suggestion"))


class ImageManagedTreeTest(unittest.TestCase):
    """The file-tool half: which targets sit under a home's skills/ or scripts/."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.home = self.tmp / "profiles" / "platform"
        for sub in ("skills/gke-basics", "scripts", "scratch"):
            (self.home / sub).mkdir(parents=True)
        self.root = self.tmp

    def tree(self, target):
        return gate.image_managed_tree(str(target), [self.home, self.root])

    def test_targets_under_the_managed_trees(self):
        self.assertEqual(self.tree(self.home / "skills" / "gke-basics" / "SKILL.md"), str(self.home / "skills"))
        self.assertEqual(self.tree(self.home / "skills"), str(self.home / "skills"))
        self.assertEqual(self.tree(self.home / "scripts" / "x.py"), str(self.home / "scripts"))
        self.assertEqual(self.tree(self.root / "scripts" / "shared.py"), str(self.root / "scripts"))

    def test_targets_outside_them(self):
        self.assertIsNone(self.tree(self.home / "scratch" / "n.md"))
        self.assertIsNone(self.tree(self.home / "skills-notes.md"))
        self.assertIsNone(self.tree(self.tmp / "elsewhere" / "skills" / "x"))

    def test_a_symlink_the_gateway_can_see_is_followed(self):
        (self.tmp / "link").symlink_to(self.home / "skills" / "gke-basics")
        self.assertEqual(self.tree(self.tmp / "link" / "SKILL.md"), str(self.home / "skills"))

    def test_the_message_names_the_right_tool(self):
        message = gate.image_managed_write_refusal("/x/SKILL.md", "/x", verb="Patch")
        self.assertTrue(message.startswith("Patch denied"))
        self.assertIn("skill_manage", message)
        self.assertIn("your result", message)


class CheckRootsTest(unittest.TestCase):
    """The platform-stage entry point proves the roots name real trees."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.p = self.tmp / "platform-template" / "skills"
        self.c = self.tmp / "cluster-template" / "skills"
        for root, name in ((self.p, "submit-suggestion"), (self.c, "gke-stall-detection")):
            (root / name).mkdir(parents=True)
            (root / name / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")

    def test_passes_when_every_root_holds_a_shipped_skill(self):
        with mock.patch.multiple(gate, PLATFORM_ROOTS=(str(self.p),), CLUSTER_ROOTS=(str(self.c),)):
            self.assertEqual(gate.check_roots(["--check-roots"]), 0)

    def test_fails_when_a_root_is_missing_or_empty(self):
        with mock.patch.multiple(gate, PLATFORM_ROOTS=(str(self.tmp / "nowhere"),), CLUSTER_ROOTS=(str(self.c),)):
            self.assertEqual(gate.check_roots(["--check-roots"]), 1)
        (self.c / "gke-stall-detection" / "SKILL.md").unlink()
        with mock.patch.multiple(gate, PLATFORM_ROOTS=(str(self.p),), CLUSTER_ROOTS=(str(self.c),)):
            self.assertEqual(gate.check_roots(["--check-roots"]), 1)

    def test_needs_the_flag(self):
        self.assertEqual(gate.check_roots([]), 2)


class TouchedNamesTest(unittest.TestCase):
    """The three routes into a shipped skill's directory all surface its name."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.root)], check=False))
        self.skills = self.root / "skills"
        (self.skills / "fleet-audit" / "scripts").mkdir(parents=True)
        (self.skills / "fleet-audit" / "scripts" / "SKILL.md").write_text("nested\n")

    def names(self, name, category=None, located=None):
        return gate.touched_skill_names(
            name, category, locate=lambda n: located, skills_root=lambda: self.skills)

    def test_the_bare_and_categorized_name(self):
        self.assertEqual(self.names("gke-basics"), {"gke-basics"})
        self.assertEqual(self.names("ops/gke-basics"), {"ops", "gke-basics"})
        self.assertEqual(self.names("gke-basics/scripts"), {"gke-basics", "scripts"})

    def test_the_category_is_a_route_under_the_skills_root(self):
        # create(name="scripts", category="fleet-audit") writes skills/fleet-audit/scripts.
        self.assertIn("fleet-audit", self.names("scripts", category="fleet-audit"))
        self.assertIn("fleet-audit", self.names("scripts", category="fleet-audit/deeper"))

    def test_where_an_existing_skill_lives_is_a_route(self):
        # delete(name="scripts") resolves, by directory name, to the nested one.
        located = self.skills / "fleet-audit" / "scripts"
        self.assertIn("fleet-audit", self.names("scripts", located=located))
        # A skill living outside the root (an external skills dir) contributes only its own name.
        elsewhere = self.root / "external" / "scripts"
        self.assertEqual(self.names("scripts", located=elsewhere), {"scripts"})

    def test_lookups_that_fail_do_not_break_the_answer(self):
        def boom(_name):
            raise RuntimeError("no hermes")
        with mock.patch.object(gate, "_locate", boom):
            self.assertEqual(gate.touched_skill_names("gke-basics", locate=gate._locate,
                                                      skills_root=lambda: None), {"gke-basics"})


class ApplierTest(unittest.TestCase):
    def write_tree(self, source: str, safety: str = SAFETY_UPSTREAM) -> tuple[pathlib.Path, pathlib.Path]:
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(root)], check=False))
        target = root / "tools" / "skill_manager_tool.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
        (root / "agent").mkdir()
        (root / "agent" / "__init__.py").write_text("")
        (root / "agent" / "file_safety.py").write_text(safety)
        return root, target

    def test_applier_patches_cleanly_and_parses(self):
        root, target = self.write_tree(UPSTREAM)
        apply_skill_manage_image_owned.apply(root)
        patched = target.read_text()
        ast.parse(patched)
        self.assertIn(apply_skill_manage_image_owned.MARKER, patched)
        # The batch gate sits before the batch dispatch, the flat gate after it and before the preflight.
        self.assertLess(patched.index("image_owned_batch_refusal(operations"), patched.index("return _skill_manage_batch("))
        self.assertLess(patched.index("return _skill_manage_batch("), patched.index("image_owned_refusal(action, name, category=category)"))
        self.assertLess(patched.index("image_owned_refusal(action, name, category=category)"),
                        patched.index("if (preflight := _background_review_preflight(action, name))"))

    def test_the_file_guard_is_patched_too(self):
        root, _ = self.write_tree(UPSTREAM)
        apply_skill_manage_image_owned.apply(root)
        patched = (root / "agent" / "file_safety.py").read_text()
        ast.parse(patched)
        self.assertIn(apply_skill_manage_image_owned.SAFETY_MARKER, patched)
        # The new category is classified before the safe-root test and answered
        # before the generic message.
        self.assertLess(patched.index('return "image_managed"'), patched.index("safe_roots = get_safe_write_roots()"))
        self.assertLess(patched.index('if denial == "image_managed":'), patched.index("is a protected system/credential file."))

    def test_the_patch_is_not_applied_twice(self):
        root, _ = self.write_tree(UPSTREAM)
        apply_skill_manage_image_owned.apply(root)
        with self.assertRaises(SystemExit) as caught:
            apply_skill_manage_image_owned.apply(root)
        self.assertIn("already patched", str(caught.exception))

    def test_a_moved_file_guard_anchor_fails_the_build(self):
        root, target = self.write_tree(UPSTREAM, safety=SAFETY_UPSTREAM.replace('return "safe_root"', 'return "outside"'))
        with self.assertRaises(SystemExit) as caught:
            apply_skill_manage_image_owned.apply(root)
        self.assertIn("found 0", str(caught.exception))

    def test_a_moved_anchor_fails_the_build(self):
        root, _ = self.write_tree(UPSTREAM.replace("if (preflight := ", "if (pre := "))
        with self.assertRaises(SystemExit) as caught:
            apply_skill_manage_image_owned.apply(root)
        self.assertIn("found 0", str(caught.exception))

    def test_verification_script_passes_against_patched_module(self):
        root, _ = self.write_tree(UPSTREAM)
        apply_skill_manage_image_owned.apply(root)
        patches_dir = pathlib.Path(__file__).parent.resolve()
        tools_dir = root / "tools"
        (tools_dir / "__init__.py").write_text("")
        (tools_dir / "registry.py").write_text(REGISTRY_STUB)
        (tools_dir / "skill_manage_image_owned.py").write_text(
            (patches_dir / "skill_manage_image_owned.py").read_text())
        env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        env["PYTHONPATH"] = str(root)
        proc = subprocess.run(
            [sys.executable, str(patches_dir / "verify_skill_manage_image_owned.py")],
            cwd=root, env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERIFY OK", proc.stdout)
        self.assertNotIn("FAIL", proc.stdout)


if __name__ == "__main__":
    unittest.main()
