"""Each fix recipe's end-to-end case, and every ``post_merge_outcome`` case's recipe.

A recipe's ``recipe.yaml`` names its primary end-to-end case as ``eval_case``:
the agent opens the fix, ``post_merge_outcome`` merges it, and the install's
post-merge check grades a record of that recipe's kind. Other cases may grade
the same recipe (another outcome, such as ``still_broken`` or ``not_applied``),
so the link is held one way from the recipe and only to an existing recipe from
the case: a case cannot grade a recipe that does not exist, and the case a
recipe names must grade that recipe.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RECIPES = REPO / "agents" / "platform" / "scripts" / "fix_recipes"
TASKS = REPO / "bench" / "tasks"
LINE = re.compile(r"([a-z_]+):[ \t]+(.*)")


def _recipe_fields(path: Path) -> dict[str, str]:
    """The flat ``key: value`` lines of a recipe.yaml (the registry's own grammar), quotes off."""
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LINE.fullmatch(line)
        if match and not line.startswith("#"):
            value = re.split(r"[ \t]#", match.group(2), maxsplit=1)[0].strip()
            fields[match.group(1)] = value.strip('"')
    return fields


def _post_merge_checks(node) -> list[dict]:
    if isinstance(node, list):
        return [found for child in node for found in _post_merge_checks(child)]
    if not isinstance(node, dict):
        return []
    found = [node] if node.get("type") == "post_merge_outcome" else []
    for key in ("check", "checks"):
        if key in node:
            found += _post_merge_checks(node[key])
    return found


def _cases() -> dict[str, list[dict]]:
    """Each case's post_merge_outcome checks, by case id."""
    cases = {}
    for task in sorted(TASKS.glob("*/task.yaml")):
        spec = yaml.safe_load(task.read_text(encoding="utf-8")) or {}
        checks = _post_merge_checks(spec.get("verification_spec") or [])
        if checks:
            cases[spec.get("id") or task.parent.name] = checks
    return cases


def test_every_recipe_that_names_a_case_is_graded_by_it():
    named = 0
    for path in sorted(RECIPES.glob("*/recipe.yaml")):
        fields = _recipe_fields(path)
        case = fields.get("eval_case")
        if not case:
            continue
        named += 1
        task = TASKS / case / "task.yaml"
        assert task.is_file(), f"{path.parent.name}/recipe.yaml names eval_case {case!r}, which has no {task}"
        checks = _cases().get(case, [])
        assert checks, f"{case} grades no post_merge_outcome, so it is not {fields['kind']}'s end-to-end case"
        assert fields["kind"] in {check.get("recipe") for check in checks}, (
            f"{case} grades recipe {[c.get('recipe') for c in checks]}, not {fields['kind']!r}"
        )
        assert all(check.get("apply_fixture_role") for check in checks)
    assert named, "no recipe names its end-to-end case; no-pdb's is post-merge-verified-fixed"


def test_every_post_merge_case_grades_a_recipe_that_exists():
    # Several cases may grade one recipe (#6343's cases 2 and 3 grade no-pdb
    # with other outcomes), so the case need not be the recipe's eval_case.
    recipes = {path.parent.name for path in RECIPES.glob("*/recipe.yaml")}
    for case, checks in _cases().items():
        for check in checks:
            kind = check.get("recipe")
            assert kind in recipes, f"{case} grades recipe {kind!r}, which has no fix_recipes/{kind}/recipe.yaml"


def test_recipe_check_nests_checks_under_the_case_format_keys():
    # scripts/check_recipes.py keeps its own copy, because it runs without
    # devops-bench; this holds it to the case format's.
    import sys

    from kube_agents_bench import cases

    sys.path.insert(0, str(REPO / "scripts"))
    import check_recipes

    assert set(check_recipes.CHECK_CHILD_KEYS) == set(cases._CHECK_CHILD_KEYS)
