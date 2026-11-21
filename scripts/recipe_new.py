#!/usr/bin/env python3
"""Scaffold a fix recipe: `make recipe-new KIND=<check-id>`.

Writes agents/platform/scripts/fix_recipes/<kind>/ from scripts/recipe_templates/:
recipe.yaml, fix.py, checks.py and test_recipe.py. Every place the author must
fill in carries the marker RECIPE-TEMPLATE, and `make recipe-check` fails while
any is left, so a half-written recipe cannot merge. Refuses a kind that is not
a check id, and a directory that already exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "scripts" / "recipe_templates"
RECIPES = REPO / "agents" / "platform" / "scripts" / "fix_recipes"
FILES = ("recipe.yaml", "fix.py", "checks.py", "test_recipe.py")
MARKER = "RECIPE-TEMPLATE"

sys.path.insert(0, str(RECIPES.parent))
from fix_recipes import registry


def scaffold(kind: str, recipes: Path = RECIPES) -> list[Path]:
    """Write the recipe's files and return their paths; ValueError when it cannot."""
    if not registry.KIND_RE.fullmatch(kind):
        raise ValueError(f"{kind!r} is not a check id such as `no-pdb`")
    target = recipes / kind
    if target.exists():
        raise ValueError(f"{target} already exists")
    target.mkdir()
    written = []
    for name in FILES:
        text = (TEMPLATES / f"{name}.tmpl").read_text(encoding="utf-8").replace("{kind}", kind)
        path = target / name
        path.write_text(text, encoding="utf-8")
        if name.startswith("test_"):
            path.chmod(0o755)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", help="the audit check id that finds the problem, e.g. blocking-pdb")
    args = parser.parse_args(argv)
    try:
        written = scaffold(args.kind)
    except ValueError as exc:
        print(f"recipe-new: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"wrote {path.relative_to(REPO)}")
    print(f"Fill in every {MARKER} line, give the recipe an eval case that carries "
          f"`recipe: {args.kind}`, then run `make recipe-check`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
