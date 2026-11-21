"""The stock verification probes a recipe names in its `recipe.yaml` (`applied`, `health`).

The post-merge check (`post_merge_check.watch_health`) asks :func:`applied`
whether a merged fix is on the cluster when no GitOps reconciler can say, and
reads :func:`holds_health_window` to decide whether to hold the health window
on the record's workload. Each probe reads through the check's own
``kubectl_json(record, argv)``, so it reads only the record's cluster, in the
shell sandbox, and an object kubectl reports gone raises the check's
``WorkloadGone``.

Stdlib only, like the registry.
"""

from __future__ import annotations

from collections.abc import Callable

#: ``kubectl_json(record, argv)`` -> the parsed ``-o json`` answer (``post_merge_check.KubectlJson``).
KubectlJson = Callable[[dict, list], dict]
#: ``selects(selector, labels)`` -> whether a label selector matches the labels (``post_merge_check.selects``).
#: Passed in, so this package, which the shell sandbox also carries, imports nothing it lacks.
Selects = Callable[[dict, dict], bool]
#: The health probe that passes the window once the fix is applied.
HEALTH_NONE = "none"


def holds_health_window(recipe) -> bool:
    """Whether the post-merge check holds a health window for this kind (``health: restart_flat``)."""
    return getattr(recipe, "health", "restart_flat") != HEALTH_NONE


def _noun(recipe) -> str:
    return recipe.applied_noun or "object"


def _field(document: dict, path: str) -> tuple[bool, object]:
    """``(found, value)`` at the dotted ``path`` in ``document``."""
    value: object = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _scalar_text(value: object) -> str | None:
    """A scalar as the text a recipe's ``applied_value`` would spell it, or None for a list or map."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def applied(recipe, kubectl_json: KubectlJson, record: dict, workload: dict | None,
            selects: Selects) -> tuple[bool, str]:
    """``(applied, reading)``: whether the fix ``recipe`` declares is on the record's cluster.

    ``workload`` is the record's workload as read, or None when the record names
    an object that is not one.
    """
    namespace = record["namespace"] or "default"
    if recipe is None or not recipe.applied:
        return False, f"a {record['kind']} fix cannot be observed directly"
    name = str(record["object"] or "").partition("/")[2]
    if recipe.applied == "object_present":
        found = kubectl_json(record, ["get", recipe.applied_resource, "-n", namespace, "-o", "json"])
        if workload is not None:
            labels = (((workload.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
            for item in found.get("items", []):
                if selects((item.get("spec") or {}).get("selector") or {}, labels):
                    return True, f"{_noun(recipe)} {item['metadata']['name']} selects the workload"
            return False, f"no {_noun(recipe)} selects the workload yet"
        for item in found.get("items", []):
            if (item.get("metadata") or {}).get("name") == name:
                return True, f"{_noun(recipe)} {name} is present"
        return False, f"no {_noun(recipe)} {name} yet"
    if not name:
        return False, f"{record['object']} names no object to read"
    document = kubectl_json(record, ["get", recipe.applied_resource, name, "-n", namespace, "-o", "json"])
    present, value = _field(document, recipe.applied_field)
    text = _scalar_text(value) if present else None
    if text == recipe.applied_value:
        return True, f"{_noun(recipe)} {name} has {recipe.applied_field} {recipe.applied_value}"
    shown = "unset" if not present else (text if text is not None else "not a scalar")
    return False, f"{_noun(recipe)} {name} has {recipe.applied_field} {shown}, not {recipe.applied_value} yet"
