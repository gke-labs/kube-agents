#!/usr/bin/env python3
"""Validate the fix recipes: `make recipe-check`.

For every agents/platform/scripts/fix_recipes/<kind>/:

- the registry loads it (fields, values, directory named after its kind);
- its `eval_case` names a bench/tasks/<case>/task.yaml whose
  `post_merge_outcome` check carries `recipe: <kind>` (the one place a case
  names its recipe). Other cases may grade the same recipe, but every case's
  `recipe` must be a recipe that exists, so renaming or deleting a recipe
  cannot strand its cases (bench/tests/test_recipe_eval_cases.py holds the
  same link for the bench suite);
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
REQUIRED_NAMES = {"fix.py": ("generate", "read_inputs"), "checks.py": ("read_kinds", "checks", "unanswered")}
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
        "post_merge_outcome.py",
        "post_merge_scope.py",
        "reconciler_sync.py",
        "clean_merges.py",
        "fix_recipes/__init__.py",
        "fix_recipes/registry.py",
        "fix_recipes/probes.py",
        "fix_recipes/yaml_edit.py",
    )
)

sys.path.insert(0, str(SCRIPTS))
import yaml  # noqa: E402
from fix_recipes import registry  # noqa: E402


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
            elif recipe.kind not in _graded(task):
                problems.append(
                    f"{where}: bench/tasks/{recipe.eval_case}/task.yaml has no post_merge_outcome check "
                    f"with `recipe: {recipe.kind}`"
                )
        for path in sorted((recipes_dir / recipe.kind).iterdir()):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                if path.suffix in (".py", ".yaml"):
                    problems.append(f"{where}/{path.name}: is not UTF-8 text")
                continue  # a stray binary such as .DS_Store carries no scaffold line
            if MARKER in text:
                problems.append(f"{where}/{path.name}: still has scaffold lines marked {MARKER}")
            if path.name in REQUIRED_NAMES:
                try:
                    defined = _top_level_names(text)
                except SyntaxError as exc:
                    problems.append(f"{where}/{path.name}: does not parse: {exc.msg} (line {exc.lineno})")
                    continue
                missing = [n for n in REQUIRED_NAMES[path.name] if n not in defined]
                if missing:
                    problems.append(f"{where}/{path.name}: does not define {', '.join(missing)}")
    kinds = {recipe.kind for recipe in recipes}
    for task in sorted(tasks_dir.glob("*/task.yaml")):
        for kind in sorted(_graded(task) - kinds):
            problems.append(
                f"bench/tasks/{task.parent.name}/task.yaml: its post_merge_outcome check grades recipe {kind!r}, "
                f"which has no fix_recipes/{kind}/recipe.yaml"
            )
    return problems


def _graded(task: Path) -> set[str]:
    """case_recipes, or nothing for a case that does not parse: bench-case-check reports that."""
    try:
        return case_recipes(task)
    except yaml.YAMLError:
        return set()


#: The keys a verification check nests checks under. A copy of
#: kube_agents_bench.cases._CHECK_CHILD_KEYS, which this script cannot import:
#: it runs without the bench install (devops-bench). The bench suite's
#: test_recipe_eval_cases.py holds the two equal.
CHECK_CHILD_KEYS = ("checks", "check")


def case_recipes(task: Path) -> set[str]:
    """The `recipe` of every post_merge_outcome check in a case, however deep it is nested."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            if node.get("type") == "post_merge_outcome" and isinstance(node.get("recipe"), str):
                found.add(node["recipe"])
            for key in CHECK_CHILD_KEYS:
                walk(node.get(key))

    spec = yaml.safe_load(task.read_text(encoding="utf-8")) or {}
    walk(spec.get("verification_spec") if isinstance(spec, dict) else None)
    return found


def _top_level_names(text: str) -> set[str]:
    names = set()
    for node in ast.parse(text).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    return names


def plumbing_flags(changed: list[str]) -> list[str]:
    """The shared-plumbing files a change edits alongside a recipe; empty when it touches no recipe."""
    # A recipe is a directory: fix_recipes/<kind>/<file>. The modules beside
    # the kinds (registry, probes, the YAML editor) are plumbing.
    recipe_paths = [p for p in changed if p.startswith(RECIPE_PREFIX) and "/" in p[len(RECIPE_PREFIX):]]
    if not recipe_paths:
        return []
    return sorted(p for p in changed if p in SHARED_PLUMBING)


def changed_files(base: str) -> list[str]:
    """The files changed since `base`; empty, with a warning, when git cannot say (the flag only warns)."""
    # From the merge-base, so a base branch that moved on is not counted as this change.
    result = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=REPO,
                            capture_output=True, text=True)
    if result.returncode:
        print(f"::warning::recipe-check could not diff against {base}, so it skipped the shared-plumbing flag: "
              f"{(result.stderr.strip().splitlines() or ['git diff failed'])[0]}")
        return []
    return [line for line in result.stdout.splitlines() if line]


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
