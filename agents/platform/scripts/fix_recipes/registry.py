"""The registry of fix recipes: the one list of the kinds of fix kube-agents can auto-merge.

Each kind is a directory beside this file holding a `recipe.yaml`, named after
the fleet-audit check that finds the problem (`no-pdb/recipe.yaml`). The
rules file's known kinds (`automation_rules.KNOWN_FIX_TYPES`), the dial's rows
(`dial_state.FIX_KINDS`) and the kinds the post-merge check records
(`session_kv_server.POST_MERGE_KINDS`, the `pr_opened` plugin) all read this.
A recipe's safety checks are its `checks.py`, which the broker's label gate
loads through :func:`checks_module`.
Those are the lists; the code that handles one kind's fix is still per kind
(`automerge_eligibility.FIX_TYPE`, `audit_report.GENERATED_FIX_CHECK`), so a
recipe alone gets a dial row and post-merge records but no broker label. How the
post-merge check sees the fix applied, and whether it holds a health window, is
declared here (`applied`, `health`) and run by `fix_recipes/probes.py`.

Stdlib only, because the credential proxy and the shell sandbox import it
through `automation_rules`, and neither may depend on PyYAML being there. So
`recipe.yaml` is a strict subset of YAML: one `key: value` per line, `#`
comments, a value plain or in double quotes. Anything else refuses the file,
and a refused or missing recipe is an import-time error, caught by the tests,
rather than a kind that silently drops out.

Fields: `kind` (the directory's name), `name` (what text kube-agents writes
calls the fixes), `title` and `dial_environment` (the dial's row), and the
optional `owner`, `eval_case` (the end-to-end case that grades the fix on the
cluster after merge), and `recheck_kinds` and `recheck_siblings`: the kubectl
kinds the one-finding re-check reads in the target namespace (separated by
commas, as `kubectl get` takes them), and the collector checks a fix could
trip (separated by spaces); `recheck.py` builds its
`RECHECKS` from them.

The verification fields, all optional:

- `applied`: how the post-merge check sees the fix on the cluster when no
  GitOps reconciler can say. `object_present`: an object of
  `applied_resource` in the record's namespace, one whose selector selects the
  workload when the record names a workload, else the object the record names.
  `field_equals`: the object the record names, read as `applied_resource`, has
  `applied_field` (a dotted path of plain keys: no list index, and no key with a
  `.` in it) equal to `applied_value`. That object must exist before the fix,
  since the probe reads it by name and a missing one fails the record; a fix
  that creates its object uses `object_present`. Empty: the fix is
  seen only through a reconciler. `applied_noun` names the object in the
  reading (`budget`); it defaults to `object`.
- `health`: `restart_flat` (the default) holds the health window on the
  workload the record names; `none` passes the window on the run after the fix
  is applied, for a fix whose object is not a workload.

A kind's fix generator is `fix.py` beside its `recipe.yaml`, loaded with
`generator(kind)`; `no-pdb` has one, which `audit_report._pdb_manifest` calls.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

HERE = Path(__file__).resolve().parent
RECIPE_FILE = "recipe.yaml"
# A recipe's fix generator, beside its recipe.yaml (optional until every kind has one).
FIX_FILE = "fix.py"
#: A recipe's safety checks: `read_kinds` (one namespace read) and
#: `checks(change, live)`, each result named and `pass`, `fail` or `unknown`.
CHECKS_FILE = "checks.py"
# Larger than any recipe; a cap so a stray file costs a glance to refuse.
MAX_RECIPE_BYTES = 8 * 1024
REQUIRED = ("kind", "name", "title", "dial_environment")
OPTIONAL = ("owner", "eval_case", "recheck_kinds", "recheck_siblings", "applied", "applied_resource", "applied_noun",
            "applied_field", "applied_value", "health")
#: The stock probes `fix_recipes/probes.py` runs, and the health windows the post-merge check holds.
APPLIED_PROBES = ("object_present", "field_equals")
HEALTH_PROBES = ("restart_flat", "none")
#: What `applied` needs beside it.
APPLIED_NEEDS = {"object_present": ("applied_resource",),
                 "field_equals": ("applied_resource", "applied_field", "applied_value")}
#: A kubectl resource name (`poddisruptionbudgets.policy`; a plural may hold hyphens, as a DNS label may),
#: a dotted field path (`spec.maxUnavailable`).
RESOURCE_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)*")
FIELD_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*")
# The same shapes `automation_rules` accepts for a rule's `fix` and `environments`.
KIND_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ENVIRONMENT_RE = re.compile(r"[a-z0-9]([-_a-z0-9]{0,61}[a-z0-9])?")
_KUBECTL_KIND_RE = re.compile(r"[a-z][a-z0-9]*(\.[a-z0-9.-]+)?")
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
    recheck_kinds: str = ""
    recheck_siblings: str = ""
    applied: str = ""
    applied_resource: str = ""
    applied_noun: str = ""
    applied_field: str = ""
    applied_value: str = ""
    health: str = "restart_flat"

    def siblings(self) -> tuple[str, ...]:
        """The collector checks the fix could trip, in the order the re-check runs them."""
        return tuple(self.recheck_siblings.split())


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
    if fields.get("recheck_siblings") and not fields.get("recheck_kinds"):
        raise RecipeError(f"{where}: `recheck_siblings` needs `recheck_kinds`")
    if not all(KIND_RE.fullmatch(s) for s in fields.get("recheck_siblings", "").split()):
        raise RecipeError(f"{where}: `recheck_siblings` must be check ids separated by spaces")
    if not all(_KUBECTL_KIND_RE.fullmatch(k) for k in fields.get("recheck_kinds", "").split(",") if fields.get("recheck_kinds")):
        raise RecipeError(f"{where}: `recheck_kinds` must be kubectl resource names separated by commas")
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

def generator(kind: str, root: Path = HERE) -> ModuleType:
    """The `fix.py` of `kind`'s recipe, loaded once per file.

    Loaded from its path: a kind is a check id such as `no-pdb`, and a hyphen
    is not an importable package name. RecipeError for a kind with no valid
    recipe or no `fix.py`.
    """
    if not KIND_RE.fullmatch(kind) or not (root / kind / RECIPE_FILE).is_file():
        raise RecipeError(f"no recipe for kind {kind!r}")
    if kind not in {found.kind for found in (recipes() if root == HERE else load(root))}:
        raise RecipeError(f"no recipe for kind {kind!r}")
    path = root / kind / FIX_FILE
    if not path.is_file():
        raise RecipeError(f"{kind}/{FIX_FILE}: missing")
    return _load_fix(path.resolve(), kind)


@functools.cache
def _load_fix(path: Path, kind: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"fix_recipes.{kind.replace('-', '_')}_fix", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def checks_module(kind: str) -> ModuleType | None:
    """The `checks.py` of a known kind, imported once; None for a kind whose recipe ships none.

    Loaded from its file rather than imported by name, because a kind is a
    directory named like `no-pdb`, which no `import` statement can name.
    """
    if kind not in kinds():
        raise RecipeError(f"{kind!r} is not a known kind of fix")
    path = HERE / kind / CHECKS_FILE
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"fix_recipes.{kind.replace('-', '_')}.checks", path)
    if spec is None or spec.loader is None:
        raise RecipeError(f"{kind}/{CHECKS_FILE} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("read_kinds", "checks"):
        if not hasattr(module, name):
            raise RecipeError(f"{kind}/{CHECKS_FILE} has no `{name}`")
    return module
