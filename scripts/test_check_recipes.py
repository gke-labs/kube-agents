#!/usr/bin/env python3
"""The recipe scaffold and validator: `make recipe-new`, `make recipe-check`, and the bench `recipe:` key.

Run: python3 scripts/test_check_recipes.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_recipes
import recipe_new

RECIPE = "kind: {kind}\nname: n fixes\ntitle: N\ndial_environment: dev\neval_case: {case}\n"


class Tree:
    """A recipes directory and a bench tasks directory, in a temporary tree."""

    def __init__(self, root: Path) -> None:
        self.recipes = root / "fix_recipes"
        self.tasks = root / "bench" / "tasks"
        self.recipes.mkdir()
        self.tasks.mkdir(parents=True)

    def recipe(self, kind: str, case: str = "case-a", extra: dict | None = None) -> Path:
        (self.recipes / kind).mkdir()
        (self.recipes / kind / "recipe.yaml").write_text(RECIPE.format(kind=kind, case=case or '""'))
        for name, text in (extra or {}).items():
            (self.recipes / kind / name).write_text(text)
        return self.recipes / kind

    def case(self, name: str, recipe: str | None) -> None:
        """A case whose post_merge_outcome check, nested as no-pdb's is, names `recipe`."""
        (self.tasks / name).mkdir()
        line = f"\n          recipe: {recipe}" if recipe else ""
        (self.tasks / name / "task.yaml").write_text(
            f"id: {name}\nverification_spec:\n  - name: verified\n    check:\n      checks:\n"
            f"        - type: post_merge_outcome\n          outcome: verified_fixed{line}\n"
        )

    def problems(self) -> list[str]:
        return check_recipes.recipe_problems(self.recipes, self.tasks)


class RecipeCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Tree(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_recipe_whose_case_names_it_back_passes(self):
        self.tree.recipe("no-pdb")
        self.tree.case("case-a", "no-pdb")
        self.assertEqual(self.tree.problems(), [])

    def test_the_eval_case_must_exist_and_name_the_recipe(self):
        self.tree.recipe("no-pdb", case="")
        self.tree.recipe("blocking-pdb", case="missing-case")
        self.tree.recipe("hpa-floors-at-one", case="case-b")
        self.tree.case("case-b", "no-pdb")
        problems = "\n".join(self.tree.problems())
        self.assertIn("no-pdb: no `eval_case`", problems)
        self.assertIn("blocking-pdb: eval case 'missing-case' has no", problems)
        self.assertIn("case-b/task.yaml has no post_merge_outcome check with `recipe: hpa-floors-at-one`", problems)

    def test_scaffold_lines_and_missing_names_fail(self):
        self.tree.case("case-a", "no-pdb")
        self.tree.recipe("no-pdb", extra={
            "fix.py": "def generate(candidate, finding_id):\n    pass\n",
            "checks.py": "read_kinds = 'pods'\n# RECIPE-TEMPLATE\ndef checks(change, live):\n    return []\n"
                         "def unanswered(detail):\n    return []\n",
        })
        problems = "\n".join(self.tree.problems())
        self.assertIn("fix.py: does not define read_inputs", problems)
        self.assertIn("checks.py: still has scaffold lines", problems)
        self.assertNotIn("checks.py: does not define", problems)

    def test_an_unreadable_recipe_is_reported(self):
        (self.tree.recipes / "no-pdb").mkdir()
        (self.tree.recipes / "no-pdb" / "recipe.yaml").write_text("kind: no-pdb\n")
        self.assertEqual(len(self.tree.problems()), 1)

    def test_the_shipped_recipes_pass(self):
        self.assertEqual(check_recipes.recipe_problems(), [])

    def test_the_required_names_are_what_the_broker_and_the_registry_load(self):
        # registry.checks_module refuses a checks.py without the first two,
        # and the broker calls fix.read_inputs then fix.generate.
        # fix_preflight also calls checks.unanswered.
        self.assertEqual(check_recipes.REQUIRED_NAMES["checks.py"], ("read_kinds", "checks", "unanswered"))
        self.assertEqual(set(check_recipes.REQUIRED_NAMES["fix.py"]), {"generate", "read_inputs"})


class PlumbingTest(unittest.TestCase):
    RECIPE_FILE = "agents/platform/scripts/fix_recipes/blocking-pdb/recipe.yaml"

    def test_a_recipe_change_that_edits_shared_plumbing_is_flagged(self):
        changed = [self.RECIPE_FILE, "agents/platform/scripts/dial_state.py", "README.md"]
        self.assertEqual(check_recipes.plumbing_flags(changed), ["agents/platform/scripts/dial_state.py"])

    def test_plumbing_alone_or_a_recipe_alone_is_not_flagged(self):
        self.assertEqual(check_recipes.plumbing_flags(["agents/platform/scripts/dial_state.py"]), [])
        self.assertEqual(check_recipes.plumbing_flags([self.RECIPE_FILE]), [])
        # The modules beside the kinds are plumbing, not a recipe.
        for module in ("registry.py", "probes.py", "yaml_edit.py", "__init__.py"):
            path = f"agents/platform/scripts/fix_recipes/{module}"
            with self.subTest(module=module):
                self.assertEqual(check_recipes.plumbing_flags([path, "agents/platform/scripts/dial_state.py"]), [])
                self.assertEqual(check_recipes.plumbing_flags([self.RECIPE_FILE, path]), [path])


class ScaffoldTest(unittest.TestCase):
    def test_it_writes_the_four_files_and_the_check_holds_them_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Tree(Path(tmp))
            written = recipe_new.scaffold("blocking-pdb", tree.recipes)
            self.assertEqual([p.name for p in written], list(recipe_new.FILES))
            text = (tree.recipes / "blocking-pdb" / "recipe.yaml").read_text()
            self.assertIn("kind: blocking-pdb", text)
            self.assertNotIn("{kind}", "".join(p.read_text() for p in written))
            problems = tree.problems()
            self.assertTrue(any("no `eval_case`" in p for p in problems))
            self.assertEqual(sum("scaffold lines" in p for p in problems), len(recipe_new.FILES))
            # The scaffold defines everything the loaders need; only the marker holds it back.
            self.assertFalse([p for p in problems if "does not define" in p])

    def test_it_refuses_a_bad_kind_and_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Tree(Path(tmp))
            with self.assertRaises(ValueError):
                recipe_new.scaffold("Bad_Kind", tree.recipes)
            recipe_new.scaffold("blocking-pdb", tree.recipes)
            with self.assertRaises(ValueError):
                recipe_new.scaffold("blocking-pdb", tree.recipes)


class ScaffoldThroughTheLoadersTest(unittest.TestCase):
    """A freshly scaffolded recipe loads through the code that runs it, so the templates cannot drift from it."""

    def test_the_registry_the_broker_and_the_preflight_load_the_scaffold(self):
        from unittest import mock

        sys.path.insert(0, str(check_recipes.REPO / "agents/platform/skills/fleet-audit/scripts"))
        import fix_preflight
        from fix_recipes import registry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe_new.scaffold("zz-scaffold-probe", root)
            loaded = registry.load(root)
            registry.checks_module.cache_clear()
            try:
                with mock.patch.object(registry, "HERE", root), mock.patch.object(registry, "recipes", lambda: loaded):
                    # The broker's generator: what automerge_eligibility calls.
                    fix = registry.generator("zz-scaffold-probe", root)
                    self.assertTrue(callable(fix.generate) and callable(fix.read_inputs))
                    with self.assertRaises(NotImplementedError):
                        fix.generate({}, "finding")
                    # The safety checks: the registry's own loader refuses a module missing a name.
                    checks = registry.checks_module("zz-scaffold-probe")
                    self.assertTrue(callable(checks.checks))
                    # The pre-flight runs the template's `unanswered` and reads its result shape.
                    answer = fix_preflight.check_fix({"check": "zz-scaffold-probe", "id": "f"}, None, in_manifest=True)
                    self.assertFalse(answer["eligible"])
                    for result in answer["checks"]:
                        self.assertEqual(set(result), {"name", "passed", "detail", "status"})
                        self.assertEqual(result["status"], "unknown")
            finally:
                registry.checks_module.cache_clear()


if __name__ == "__main__":
    unittest.main()
