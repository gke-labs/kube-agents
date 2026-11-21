"""Each fix recipe's end-to-end case: the one ``bench/tasks`` case its ``recipe.yaml`` names as ``eval_case``.

A recipe that names a case gets one, not four (detect, fix, verify and refuse in
one run): the agent opens the fix, ``post_merge_outcome`` merges and applies it,
and the install's post-merge check must reach ``verified_fixed`` for a record
of that recipe's kind. This pins the link both ways, so a case cannot grade one
recipe while its ``recipe.yaml`` names it for another.
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
        assert {check.get("recipe") for check in checks} == {fields["kind"]}, (
            f"{case} grades recipe {[c.get('recipe') for c in checks]}, not {fields['kind']!r}"
        )
        assert all(check.get("outcome") == "verified_fixed" and check.get("apply_fixture_role") for check in checks)
        edits = fields.get("applied") == "field_equals"
        assert all(bool(check.get("apply_edits")) == edits for check in checks), (
            f"{case} must set apply_edits: {str(edits).lower()} for {fields['kind']}, whose applied probe is "
            f"{fields.get('applied') or 'unset'}"
        )
    assert named, "no recipe names its end-to-end case; no-pdb's is post-merge-verified-fixed"


def test_every_post_merge_case_is_named_by_its_recipe():
    recipes = {path.parent.name: _recipe_fields(path) for path in RECIPES.glob("*/recipe.yaml")}
    for case, checks in _cases().items():
        for check in checks:
            kind = check.get("recipe")
            assert kind in recipes, f"{case} grades recipe {kind!r}, which has no fix_recipes/{kind}/recipe.yaml"
            assert recipes[kind].get("eval_case") == case, (
                f"{case} grades {kind!r}, but fix_recipes/{kind}/recipe.yaml names eval_case "
                f"{recipes[kind].get('eval_case')!r}"
            )
