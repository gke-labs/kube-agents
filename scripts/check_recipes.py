#!/usr/bin/env python3
"""Validate the fix recipes: `make recipe-check`.

For every agents/platform/scripts/fix_recipes/<kind>/:

- the registry loads it (fields, values, directory named after its kind);
- its `eval_case` names a bench/tasks/<case>/task.yaml that carries
  `recipe: <kind>` (validate_bench_cases.py checks the reverse: a case naming a
  recipe is that recipe's case);
- no file in it still carries the scaffold's RECIPE-TEMPLATE marker, and a
  `fix.py` or `checks.py` defines what the scaffold says it must.

With `--base <commit>` it also compares the change against that commit. A
change that adds or edits a recipe and also edits the shared plumbing every
recipe relies on (the dial, the rules file, the broker's label gate, the
post-merge loop, the clean-merge counter, the offer, step-down, the registry)
is flagged: a recipe should not need it, so a platform owner reviews that pull
request. Flagged, not failed, unless `--strict`.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "agents" / "platform" / "scripts"
RECIPES = SCRIPTS / "fix_recipes"
TASKS = REPO / "bench" / "tasks"
MARKER = "RECIPE-TEMPLATE"
#: What a recipe module must define, when it has one.
REQUIRED_NAMES = {"fix.py": ("generate",), "checks.py": ("READ_KINDS", "checks")}
RECIPE_PREFIX = "agents/platform/scripts/fix_recipes/"
SHARED_PLUMBING = tuple(
    f"agents/platform/scripts/{name}"
    for name in (
        "dial_state.py",
        "automation_rules.py",
        "vcs_broker.py",
        "automerge_eligibility.py",
        "automerge_switch.py",
        "post_merge_check.py",
        "post_merge_report.py",
        "post_merge_environment.py",
        "post_merge_edited.py",
        "post_merge_offer.py",
        "post_merge_stepdown.py",
        "post_merge_followups.py",
        "clean_merges.py",
        "fix_recipes/registry.py",
    )
)

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "scripts"))
import case_owner
from fix_recipes import registry


def recipe_problems(recipes_dir: Path = RECIPES, tasks_dir: Path = TASKS) -> list[str]:
    """Every problem with the recipes under `recipes_dir`, as `<where>: <what>` lines."""
    try:
        recipes = registry.load(recipes_dir)
    except registry.RecipeError as exc:
        return [str(exc)]
    problems = []
    for recipe in recipes:
        where = f"fix_recipes/{recipe.kind}"
        if not recipe.eval_case:
            problems.append(f"{where}: no `eval_case`; name the end-to-end case that grades the fix after merge")
        else:
            task = tasks_dir / recipe.eval_case / "task.yaml"
            if not task.is_file():
                problems.append(f"{where}: eval case {recipe.eval_case!r} has no bench/tasks/{recipe.eval_case}/task.yaml")
            elif case_owner.task_field(task.read_text(encoding="utf-8"), "recipe") != recipe.kind:
                problems.append(f"{where}: bench/tasks/{recipe.eval_case}/task.yaml does not carry `recipe: {recipe.kind}`")
        for path in sorted((recipes_dir / recipe.kind).iterdir()):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if MARKER in text:
                problems.append(f"{where}/{path.name}: still has scaffold lines marked {MARKER}")
            if path.name in REQUIRED_NAMES:
                defined = _top_level_names(text)
                missing = [n for n in REQUIRED_NAMES[path.name] if n not in defined]
                if missing:
                    problems.append(f"{where}/{path.name}: does not define {', '.join(missing)}")
    return problems


def _top_level_names(text: str) -> set[str]:
    names = set()
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def plumbing_flags(changed: list[str]) -> list[str]:
    """The shared-plumbing files a change edits alongside a recipe; empty when it touches no recipe."""
    recipe_paths = [p for p in changed if p.startswith(RECIPE_PREFIX) and p not in SHARED_PLUMBING]
    if not recipe_paths:
        return []
    return sorted(p for p in changed if p in SHARED_PLUMBING)


def changed_files(base: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", base, "HEAD"], cwd=REPO, check=True,
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="", help="flag shared-plumbing edits made alongside a recipe since this commit")
    parser.add_argument("--strict", action="store_true", help="fail, rather than flag, on such an edit")
    args = parser.parse_args(argv)
    problems = recipe_problems()
    for problem in problems:
        print(f"recipe-check: {problem}")
    flagged = plumbing_flags(changed_files(args.base)) if args.base else []
    for path in flagged:
        print(f"::warning file={path}::this change edits a recipe and shared plumbing ({path}); "
              "a recipe should not need it, so a platform owner reviews this pull request")
    if problems or (args.strict and flagged):
        return 1
    print(f"recipe-check: {len(registry.load(RECIPES))} recipe(s) ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
