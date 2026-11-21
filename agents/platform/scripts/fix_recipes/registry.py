"""The registry of fix recipes: the one list of the kinds of fix kube-agents can auto-merge.

Each kind is a directory beside this file holding a `recipe.yaml`, named after
the fleet-audit check that finds the problem (`no-pdb/recipe.yaml`). The
rules file's known kinds (`automation_rules.KNOWN_FIX_TYPES`), the dial's rows
(`dial_state.FIX_KINDS`) and the kinds the post-merge check records
(`session_kv_server.POST_MERGE_KINDS`, the `pr_opened` plugin) all read this.
A recipe's safety checks are its `checks.py`, which the broker's label gate
and the sandbox's pre-flight (`fleet-audit/scripts/fix_preflight.py`) both load
through :func:`checks_module`.
Those are the lists; the code that handles one kind's fix is still per kind
(`automerge_eligibility.FIX_TYPE`, `audit_report.GENERATED_FIX_CHECK`,
`fix_preflight.CHANGES`), so a recipe alone gets a dial row and post-merge
records but no broker label. How the post-merge check sees the fix applied, and
whether it holds a health window, is declared here (`applied`, `health`) and run
by `fix_recipes/probes.py`.

Stdlib only, because the credential proxy and the shell sandbox import it
(through `automation_rules`, and the proxy also through
`automerge_eligibility`), and neither may depend on PyYAML being there. So
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

How the broker proves a pull request is the fix: `verify` is `add-manifest`
(the default: one new file, byte for byte what `fix.py` generates) or
`edit-manifest`. An `edit-manifest` recipe names `edit_kind`, the Kubernetes
kind of the one document it edits, and `edit_paths`, the dotted scalar paths
it may change (a segment of digits is a list index), never an identity,
label, selector, ownership or status field (`EDIT_FORBIDDEN_SEGMENTS`).
`automerge_edit` holds a pull request to exactly those edits, refuses any
that changes a field's YAML type (a bool is not an int), and then asks
`fix.py`'s `allowed_edit(path, old, new)`, which must return True for each.

A kind's fix generator is `fix.py` beside its `recipe.yaml`, loaded with
`generator(kind)`, and stdlib only for the same reason. `no-pdb` has one:
the audit calls it through `audit_report._pdb_manifest`, and the broker
re-runs it to verify a pull request
(`automerge_eligibility.verify_added_manifest`).
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
            "applied_field", "applied_value", "health", "verify", "edit_kind", "edit_paths")
#: The stock probes `fix_recipes/probes.py` runs, and the health windows the post-merge check holds.
APPLIED_PROBES = ("object_present", "field_equals")
HEALTH_PROBES = ("restart_flat", "none")
#: How the broker proves a pull request is the recipe's fix: `add-manifest`
#: (one new file, `automerge_eligibility.verify_added_manifest`) or
#: `edit-manifest` (one scalar edit to an existing manifest, `automerge_edit`).
VERIFY_VERBS = ("add-manifest", "edit-manifest")
#: The object fields an edit may never touch (`yaml_edit.PROTECTED_PATHS`).
EDIT_FORBIDDEN = ("apiVersion", "kind", "metadata.name", "metadata.namespace")
#: Segments refused anywhere in an `edit_paths` entry: identity at any depth
#: (a List's items, a pod template), what an object selects or is selected
#: by, ownership, and status, which the cluster writes.
EDIT_FORBIDDEN_SEGMENTS = frozenset({
    "apiVersion", "kind", "name", "namespace", "generateName", "uid", "ownerReferences", "finalizers",
    "labels", "annotations", "selector", "matchLabels", "matchExpressions", "status",
})
#: One `edit_paths` entry: dotted mapping keys, a segment of digits being a list index.
EDIT_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*))*")
_K8S_KIND_RE = re.compile(r"[A-Z][A-Za-z0-9]*")
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
    verify: str = "add-manifest"
    edit_kind: str = ""
    edit_paths: str = ""

    def edit_path_list(self) -> tuple[tuple[str | int, ...], ...]:
        """`edit_paths` as `yaml_edit` paths: `spec.containers.0.image` is
        `("spec", "containers", 0, "image")`."""
        return tuple(
            tuple(int(seg) if seg.isdigit() else seg for seg in path.split("."))
            for path in self.edit_paths.split()
        )

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
    verify = fields.get("verify", "add-manifest")
    if verify not in VERIFY_VERBS:
        raise RecipeError(f"{where}: `verify` must be one of {', '.join(VERIFY_VERBS)}")
    if verify != "edit-manifest":
        if "edit_kind" in fields or "edit_paths" in fields:
            raise RecipeError(f"{where}: `edit_kind` and `edit_paths` mean nothing without `verify: edit-manifest`")
        return
    if not _K8S_KIND_RE.fullmatch(fields.get("edit_kind", "")):
        raise RecipeError(f"{where}: `verify: edit-manifest` needs `edit_kind`, a Kubernetes kind such as `PodDisruptionBudget`")
    paths = fields.get("edit_paths", "").split()
    if not paths or not all(EDIT_PATH_RE.fullmatch(p) for p in paths):
        raise RecipeError(f"{where}: `edit_paths` must be dotted field paths separated by spaces, such as `spec.minAvailable`")
    if len(set(paths)) != len(paths):
        raise RecipeError(f"{where}: `edit_paths` repeats a path")
    named = sorted({seg for p in paths for seg in p.split(".") if seg in EDIT_FORBIDDEN_SEGMENTS})
    if named:
        raise RecipeError(f"{where}: `edit_paths` may not name {', '.join(named)} at any depth")
    if any(a != b and b.startswith(a + ".") for a in paths for b in paths):
        raise RecipeError(f"{where}: one `edit_paths` entry contains another")


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
