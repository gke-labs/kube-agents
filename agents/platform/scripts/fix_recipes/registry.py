"""The registry of fix recipes: the one list of the kinds of fix kube-agents can auto-merge.

Each kind is a directory beside this file holding a `recipe.yaml`, named after
the fleet-audit check that finds the problem (`no-pdb/recipe.yaml`). The
rules file's known kinds (`automation_rules.KNOWN_FIX_TYPES`), the dial's rows
(`dial_state.FIX_KINDS`) and the kinds the post-merge check records
(`session_kv_server.POST_MERGE_KINDS`, the `pr_opened` plugin) all read this,
so a kind is added in one place.

Stdlib only, because the credential proxy and the shell sandbox import it
through `automation_rules`, and neither may depend on PyYAML being there. So
`recipe.yaml` is a strict subset of YAML: one `key: value` per line, `#`
comments, a value plain or in double quotes. Anything else refuses the file,
and a refused or missing recipe is an import-time error, caught by the tests,
rather than a kind that silently drops out.

Fields: `kind` (the directory's name), `name` (what text kube-agents writes
calls the fixes), `title` and `dial_environment` (the dial's row), and the
optional `owner` and `eval_case` (the end-to-end case that grades the fix on
the cluster after merge).
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
RECIPE_FILE = "recipe.yaml"
# Larger than any recipe; a cap so a stray file costs a glance to refuse.
MAX_RECIPE_BYTES = 8 * 1024
REQUIRED = ("kind", "name", "title", "dial_environment")
OPTIONAL = ("owner", "eval_case")
# The same shapes `automation_rules` accepts for a rule's `fix` and `environments`.
KIND_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ENVIRONMENT_RE = re.compile(r"[a-z0-9]([-_a-z0-9]{0,61}[a-z0-9])?")
_LINE_RE = re.compile(r"([a-z_]+):[ \t]+(.*)")


class RecipeError(ValueError):
    """A recipe file this module refuses."""


class Recipe(NamedTuple):
    kind: str
    name: str
    title: str
    dial_environment: str
    owner: str = ""
    eval_case: str = ""


def _value(raw: str, where: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError as exc:
            raise RecipeError(f"{where}: unreadable quoted value") from exc
        rest = raw[end:].strip()
        if not isinstance(value, str) or (rest and not rest.startswith("#")):
            raise RecipeError(f"{where}: unreadable quoted value")
        return value
    value = raw.split(" #", 1)[0].strip()
    if not value or value[0] in "'[{&*!|>%@`-" or ": " in value or value.endswith(":"):
        raise RecipeError(f"{where}: only plain or double-quoted values are read")
    return value


def parse(text: str, where: str = RECIPE_FILE) -> Recipe:
    """The recipe `text` declares, or RecipeError."""
    if len(text.encode()) > MAX_RECIPE_BYTES:
        raise RecipeError(f"{where}: larger than {MAX_RECIPE_BYTES} bytes")
    fields: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.fullmatch(line)
        if match is None:
            raise RecipeError(f"{where}:{number}: expected `key: value` at the start of the line")
        key = match.group(1)
        if key not in REQUIRED + OPTIONAL:
            raise RecipeError(f"{where}:{number}: unknown key `{key}`")
        if key in fields:
            raise RecipeError(f"{where}:{number}: key `{key}` appears twice")
        fields[key] = _value(match.group(2), f"{where}:{number}")
    missing = [key for key in REQUIRED if not fields.get(key)]
    if missing:
        raise RecipeError(f"{where}: missing `{missing[0]}`")
    if not KIND_RE.fullmatch(fields["kind"]):
        raise RecipeError(f"{where}: `kind` must be a check id such as `no-pdb`")
    if not ENVIRONMENT_RE.fullmatch(fields["dial_environment"]):
        raise RecipeError(f"{where}: `dial_environment` must be an environment label value such as `dev`")
    return Recipe(**fields)


def load(root: Path = HERE) -> tuple[Recipe, ...]:
    """Every recipe under `root`, sorted by kind. A directory's name must be its recipe's kind."""
    recipes = []
    for path in sorted(root.glob(f"*/{RECIPE_FILE}")):
        recipe = parse(path.read_text(encoding="utf-8"), f"{path.parent.name}/{RECIPE_FILE}")
        if recipe.kind != path.parent.name:
            raise RecipeError(f"{path.parent.name}/{RECIPE_FILE}: `kind` is {recipe.kind!r}, not the directory's name")
        recipes.append(recipe)
    if not recipes:
        raise RecipeError(f"no {RECIPE_FILE} under {root}")
    return tuple(recipes)


@functools.lru_cache(maxsize=1)
def recipes() -> tuple[Recipe, ...]:
    """The recipes shipped beside this module, read once."""
    return load()


def kinds() -> frozenset[str]:
    return frozenset(recipe.kind for recipe in recipes())
