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
        (self.tasks / name).mkdir()
        line = f"recipe: {recipe}\n" if recipe else ""
        (self.tasks / name / "task.yaml").write_text(f"id: {name}\ndomain: reliability\n{line}")

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
        self.assertIn("case-b/task.yaml does not carry `recipe: hpa-floors-at-one`", problems)

    def test_scaffold_lines_and_missing_names_fail(self):
        self.tree.case("case-a", "no-pdb")
        self.tree.recipe("no-pdb", extra={
            "fix.py": "def other():\n    pass\n",
            "checks.py": "READ_KINDS = 'pods'\n# RECIPE-TEMPLATE\ndef checks(change, live):\n    return []\n",
        })
        problems = "\n".join(self.tree.problems())
        self.assertIn("fix.py: does not define generate", problems)
        self.assertIn("checks.py: still has scaffold lines", problems)
        self.assertNotIn("checks.py: does not define", problems)

    def test_an_unreadable_recipe_is_reported(self):
        (self.tree.recipes / "no-pdb").mkdir()
        (self.tree.recipes / "no-pdb" / "recipe.yaml").write_text("kind: no-pdb\n")
        self.assertEqual(len(self.tree.problems()), 1)

    def test_the_shipped_recipes_pass(self):
        self.assertEqual(check_recipes.recipe_problems(), [])


class PlumbingTest(unittest.TestCase):
    RECIPE_FILE = "agents/platform/scripts/fix_recipes/blocking-pdb/recipe.yaml"

    def test_a_recipe_change_that_edits_shared_plumbing_is_flagged(self):
        changed = [self.RECIPE_FILE, "agents/platform/scripts/dial_state.py", "README.md"]
        self.assertEqual(check_recipes.plumbing_flags(changed), ["agents/platform/scripts/dial_state.py"])

    def test_plumbing_alone_or_a_recipe_alone_is_not_flagged(self):
        self.assertEqual(check_recipes.plumbing_flags(["agents/platform/scripts/dial_state.py"]), [])
        self.assertEqual(check_recipes.plumbing_flags([self.RECIPE_FILE]), [])
        # The registry sits in fix_recipes/ but is plumbing, not a recipe.
        self.assertEqual(check_recipes.plumbing_flags(["agents/platform/scripts/fix_recipes/registry.py"]), [])


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

    def test_it_refuses_a_bad_kind_and_an_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Tree(Path(tmp))
            with self.assertRaises(ValueError):
                recipe_new.scaffold("Bad_Kind", tree.recipes)
            recipe_new.scaffold("blocking-pdb", tree.recipes)
            with self.assertRaises(ValueError):
                recipe_new.scaffold("blocking-pdb", tree.recipes)


class BenchRecipeKeyTest(unittest.TestCase):
    """validate_bench_cases.py's side: a case's `recipe:` names a recipe whose eval_case is that case."""

    def problems_for(self, name: str, recipe_line: str) -> list[str]:
        import validate_bench_cases as vbc

        source = (vbc.TASKS_DIR / "post-merge-verified-fixed" / "task.yaml").read_text(encoding="utf-8")
        text = source.replace("recipe: no-pdb\n", recipe_line)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name / "task.yaml"
            path.parent.mkdir()
            path.write_text(text.replace("id: post-merge-verified-fixed", f"id: {name}"), encoding="utf-8")
            return [p for p in vbc.validate_case(name, path, registered=None) if "recipe" in p]

    def test_the_shipped_case_and_recipe_agree(self):
        self.assertEqual(self.problems_for("post-merge-verified-fixed", "recipe: no-pdb\n"), [])

    def test_an_unknown_recipe_or_one_that_does_not_name_the_case_is_refused(self):
        self.assertIn("is not a fix recipe", " ".join(self.problems_for("post-merge-verified-fixed", "recipe: no-such\n")))
        self.assertIn("whose eval_case is", " ".join(self.problems_for("another-case", "recipe: no-pdb\n")))

    def test_a_recipe_line_yaml_and_the_line_scan_read_differently_is_refused(self):
        problems = self.problems_for("post-merge-verified-fixed", 'recipe: &r no-pdb\n')
        self.assertIn("reads as", " ".join(problems))


if __name__ == "__main__":
    unittest.main()
