#!/usr/bin/env python3
"""The fix-recipe registry: what a recipe.yaml may say, and the lists that read it.

Run: python3 agents/platform/scripts/test_fix_recipes.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from fix_recipes import registry

GOOD = 'kind: no-pdb\nname: disruption-budget fixes\ntitle: Disruption budgets\ndial_environment: dev\n'


class ParseTest(unittest.TestCase):
    def test_the_minimal_recipe(self):
        recipe = registry.parse(GOOD)
        self.assertEqual(recipe, registry.Recipe("no-pdb", "disruption-budget fixes", "Disruption budgets", "dev"))

    def test_comments_quotes_and_the_optional_fields(self):
        text = '# about it\n' + GOOD + 'owner: platform\t# who answers\neval_case: ""\n'
        recipe = registry.parse(text)
        self.assertEqual((recipe.owner, recipe.eval_case), ("platform", ""))

    def test_what_it_refuses(self):
        refused = {
            "unknown key": GOOD + "tier: 1\n",
            "repeated key": GOOD + "title: Again\n",
            "missing field": GOOD.replace("title: Disruption budgets\n", ""),
            "empty required field": GOOD.replace("Disruption budgets", '""'),
            "nested value": GOOD + "owner:\n  name: x\n",
            "indented key": GOOD + "  owner: platform\n",
            "flow list": GOOD + "owner: [a, b]\n",
            "single quotes": GOOD + "owner: 'platform'\n",
            "anchor": GOOD + "owner: &a platform\n",
            "a mapping hidden in a value": GOOD + "owner: a: b\n",
            "a kind that is not a check id": GOOD.replace("kind: no-pdb", "kind: No_PDB"),
            "an environment that is not a label value": GOOD.replace("dial_environment: dev", "dial_environment: Dev!"),
            "an unterminated quote": GOOD + 'owner: "platform\n',
            "text after a quote": GOOD + 'owner: "a" b\n',
            "a comment where the value goes": GOOD + "owner: # todo\n",
            "a value YAML reads as null": GOOD + "owner: null\n",
            "a value YAML reads as a bool": GOOD + "owner: true\n",
            "a value YAML reads as a number": GOOD + "owner: 12\n",
            "oversize": GOOD + "# " + "x" * registry.MAX_RECIPE_BYTES + "\n",
        }
        for why, text in refused.items():
            with self.subTest(why), self.assertRaises(registry.RecipeError):
                registry.parse(text)

    def test_it_agrees_with_pyyaml_on_what_it_accepts(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        for path in sorted(registry.HERE.glob(f"*/{registry.RECIPE_FILE}")):
            text = path.read_text(encoding="utf-8")
            loaded = {k: ("" if v is None else str(v)) for k, v in yaml.safe_load(text).items()}
            self.assertEqual(registry.parse(text)._asdict(), {**dict.fromkeys(registry.OPTIONAL, ""), **loaded})


class LoadTest(unittest.TestCase):
    def write(self, root: Path, directory: str, text: str) -> None:
        (root / directory).mkdir()
        (root / directory / registry.RECIPE_FILE).write_text(text)

    def test_a_directory_must_be_named_after_its_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(Path(tmp), "other", GOOD)
            with self.assertRaises(registry.RecipeError):
                registry.load(Path(tmp))

    def test_no_recipes_is_an_error_not_an_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(registry.RecipeError):
            registry.load(Path(tmp))

    def test_recipes_come_back_sorted_by_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(Path(tmp), "no-pdb", GOOD)
            self.write(Path(tmp), "blocking-pdb", GOOD.replace("kind: no-pdb", "kind: blocking-pdb"))
            self.assertEqual([r.kind for r in registry.load(Path(tmp))], ["blocking-pdb", "no-pdb"])


class ShippedRecipesTest(unittest.TestCase):
    def test_no_pdb_is_the_one_kind_and_reads_as_it_did_when_it_was_hard_coded(self):
        self.assertEqual(registry.kinds(), frozenset({"no-pdb"}))
        (recipe,) = registry.recipes()
        self.assertEqual((recipe.name, recipe.title, recipe.dial_environment),
                         ("disruption-budget fixes", "Disruption budgets", "dev"))

    def test_the_lists_that_used_to_be_hard_coded_read_the_registry(self):
        import automation_rules
        import dial_state

        self.assertEqual(automation_rules.KNOWN_FIX_TYPES, frozenset({"no-pdb"}))
        self.assertEqual(automation_rules.FIX_TYPE_NAMES, {"no-pdb": "disruption-budget fixes"})
        self.assertEqual(dial_state.FIX_KINDS, (("no-pdb", "Disruption budgets", "dev"),))


class ChecksModuleTest(unittest.TestCase):
    def test_no_pdb_ships_its_checks(self):
        module = registry.checks_module("no-pdb")
        self.assertTrue(callable(module.checks))
        self.assertIsInstance(module.read_kinds, str)
        self.assertIs(module, registry.checks_module("no-pdb"))

    def test_an_unknown_kind_is_an_error(self):
        with self.assertRaises(registry.RecipeError):
            registry.checks_module("no-such-kind")

if __name__ == "__main__":
    unittest.main()
