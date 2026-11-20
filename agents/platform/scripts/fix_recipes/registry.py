"""The registry of fix recipes: the one list of the kinds of fix kube-agents can auto-merge.

Each kind is a directory beside this file holding a `recipe.yaml`, named after
the fleet-audit check that finds the problem (`no-pdb/recipe.yaml`). The
rules file's known kinds (`automation_rules.KNOWN_FIX_TYPES`), the dial's rows
(`dial_state.FIX_KINDS`) and the kinds the post-merge check records
(`session_kv_server.POST_MERGE_KINDS`, the `pr_opened` plugin) all read this.
Those are the lists; the code that handles one kind's fix is still per kind
(`automerge_eligibility.FIX_TYPE`, `audit_report.GENERATED_FIX_CHECK`,
`recheck.RECHECKS`), so a recipe alone gets a dial row and post-merge records
but no broker label. How the post-merge check sees the fix applied, and
whether it holds a health window, is declared here (`applied`, `health`) and
run by `fix_recipes/probes.py`.

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

The verification fields, all optional:

- `applied`: how the post-merge check sees the fix on the cluster when no
  GitOps reconciler can say. `object_present`: an object of
  `applied_resource` in the record's namespace, one whose selector selects the
  workload when the record names a workload, else the object the record names.
  `field_equals`: the object the record names, read as `applied_resource`, has
  `applied_field` (a dotted path) equal to `applied_value`. Empty: the fix is
  seen only through a reconciler. `applied_noun` names the object in the
  reading (`budget`); it defaults to `object`.
- `health`: `restart_flat` (the default) holds the health window on the
  workload the record names; `none` passes the window once the fix is applied,
  for a fix whose object is not a workload.
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
OPTIONAL = ("owner", "eval_case", "applied", "applied_resource", "applied_noun", "applied_field", "applied_value",
            "health")
#: The stock probes `fix_recipes/probes.py` runs, and the health windows the post-merge check holds.
APPLIED_PROBES = ("object_present", "field_equals")
HEALTH_PROBES = ("restart_flat", "none")
#: What `applied` needs beside it.
APPLIED_NEEDS = {"object_present": ("applied_resource",),
                 "field_equals": ("applied_resource", "applied_field", "applied_value")}
#: A kubectl resource name (`poddisruptionbudgets.policy`), a dotted field path (`spec.maxUnavailable`).
RESOURCE_RE = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)*")
FIELD_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*")
# The same shapes `automation_rules` accepts for a rule's `fix` and `environments`.
KIND_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ENVIRONMENT_RE = re.compile(r"[a-z0-9]([-_a-z0-9]{0,61}[a-z0-9])?")
_LINE_RE = re.compile(r"([a-z_]+):[ \t]+(.*)")
# Plain values YAML reads as something other than a string: refused rather
# than kept as text a YAML reader would disagree with.
_NOT_A_STRING_RE = re.compile(r"(?i:null|~|true|false|yes|no|on|off|[-+]?(\d[\d_]*)?\.?\d+([eE][-+]?\d+)?)")


class RecipeError(ValueError):
    """A recipe file this module refuses."""


class Recipe(NamedTuple):
    kind: str
    name: str
    title: str
    dial_environment: str
    owner: str = ""
    eval_case: str = ""
    applied: str = ""
    applied_resource: str = ""
    applied_noun: str = ""
    applied_field: str = ""
    applied_value: str = ""
    health: str = "restart_flat"


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
    value = re.split(r"[ \t]#", raw, maxsplit=1)[0].strip()
    if (
        not value
        or value[0] in "#'[{&*!|>%@`-?,"
        or ": " in value
        or ":\t" in value
        or value.endswith(":")
        or _NOT_A_STRING_RE.fullmatch(value)
    ):
        raise RecipeError(f"{where}: only plain or double-quoted string values are read")
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
    _check_verification(fields, where)
    return Recipe(**fields)


def _check_verification(fields: dict[str, str], where: str) -> None:
    applied = fields.get("applied", "")
    if applied and applied not in APPLIED_PROBES:
        raise RecipeError(f"{where}: `applied` must be one of {', '.join(APPLIED_PROBES)}")
    needed = APPLIED_NEEDS.get(applied, ())
    for key in ("applied_resource", "applied_field", "applied_value"):
        if key in needed and not fields.get(key):
            raise RecipeError(f"{where}: `applied: {applied}` needs `{key}`")
        if key not in needed and key in fields:
            raise RecipeError(f"{where}: `{key}` means nothing without an `applied` probe that reads it")
    if "applied_noun" in fields and not applied:
        raise RecipeError(f"{where}: `applied_noun` means nothing without `applied`")
    if fields.get("applied_resource") and not RESOURCE_RE.fullmatch(fields["applied_resource"]):
        raise RecipeError(f"{where}: `applied_resource` must be a resource name such as `poddisruptionbudgets.policy`")
    if fields.get("applied_field") and not FIELD_PATH_RE.fullmatch(fields["applied_field"]):
        raise RecipeError(f"{where}: `applied_field` must be a dotted field path such as `spec.maxUnavailable`")
    if "health" in fields and fields["health"] not in HEALTH_PROBES:
        raise RecipeError(f"{where}: `health` must be one of {', '.join(HEALTH_PROBES)}")


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


def recipe(kind: str) -> Recipe | None:
    """The recipe for `kind`, or None for a kind the registry does not list."""
    return next((found for found in recipes() if found.kind == kind), None)
