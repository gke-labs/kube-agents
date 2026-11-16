"""A Workload Identity binding named by string waits for the cluster that creates its pool.

    python3 -m unittest tests.test_workload_identity_waits_for_cluster

A module that builds its binding's member as `serviceAccount:${var.project_id}.svc.id.goog[...]`
gives Terraform no edge to the cluster. The pool `<project>.svc.id.goog` exists only once the
project's first Workload-Identity-enabled cluster does, so on a first install into a new project
such a binding, created in parallel with the cluster, fails with "Identity Pool does not exist".
Every instantiation of such a module in the composition therefore names `module.gke_cluster` in
its `depends_on`. The composition is read with the shared HCL tokenizer, so comments, strings
and heredocs never read as syntax.
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from tests.test_terraform_module_tests import _EQUALS, _STR, _WORD, _blocks, _tokens
except ImportError:  # run from inside tests/
    from test_terraform_module_tests import _EQUALS, _STR, _WORD, _blocks, _tokens

MODULES = REPO_ROOT / "terraform" / "modules"
COMPOSITION = REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
# A member string that names the pool by interpolation rather than by a cluster reference.
POOL_BY_STRING = re.compile(r'"serviceAccount:\$\{[^}]+\}\.svc\.id\.goog\[')
LOCAL_MODULE_PREFIX = "../../modules/"
SOURCE = "source"
DEPENDS_ON = "depends_on"
LIST_OPEN, LIST_CLOSE, SEPARATOR = "[", "]", ","
CLUSTER_MODULE = "module.gke_cluster"


def modules_naming_the_pool_by_string() -> set[str]:
    return {path.parent.name for path in MODULES.glob("*/*.tf") if POOL_BY_STRING.search(path.read_text())}


def _attribute(flat: list, name: str) -> int | None:
    """The index of the token after `name =` among a block's depth-1 tokens."""
    for index in range(len(flat) - 2):
        if flat[index] == (_WORD, name) and flat[index + 1][0] == _EQUALS:
            return index + 2
    return None


def module_source(body: list) -> str | None:
    flat = [token for token, depth in body if depth == 1]
    index = _attribute(flat, SOURCE)
    return flat[index][1] if index is not None and flat[index][0] == _STR else None


def depends_on_items(body: list) -> set[str] | None:
    """The addresses in a block's `depends_on = [...]`, or None if it has none.

    The tokenizer splits a reference into words and `.`, so each item is rejoined from the
    tokens between separators at bracket depth 0: an index such as `module.x[0]` stays part
    of its item.
    """
    flat = [token for token, depth in body if depth == 1]
    index = _attribute(flat, DEPENDS_ON)
    if index is None or flat[index][1] != LIST_OPEN:
        return None
    items, current, depth = set(), "", 0
    for kind, value in flat[index + 1:]:
        if depth == 0 and value in (SEPARATOR, LIST_CLOSE):
            if current:
                items.add(current)
            current = ""
            if value == LIST_CLOSE:
                return items
            continue
        depth += {LIST_OPEN: 1, LIST_CLOSE: -1}.get(value, 0)
        current += f'"{value}"' if kind == _STR else value
    return items


def composition_modules(text: str) -> list:
    return [(labels[0], body) for labels, body in _blocks(_tokens(text), "module", 1)]


class WorkloadIdentityWaitsForClusterTest(unittest.TestCase):
    def test_a_depends_on_list_is_read_whole(self):
        full = {"google_project_service.required", "module.drift_pubsub[0]", "module.gke_cluster"}
        for lines in (
            "  depends_on = [google_project_service.required, module.drift_pubsub[0], module.gke_cluster]",
            "  depends_on = [\n    google_project_service.required,\n    module.drift_pubsub[0]\n"
            "    , module.gke_cluster\n  ]",
            "  depends_on = [\n    google_project_service.required, # the APIs, [first]\n"
            "    module.drift_pubsub[0], /* a gate */\n    module.gke_cluster,\n  ]",
        ):
            with self.subTest(lines=lines):
                [(_, body)] = composition_modules(f'module "m" {{\n  source = "x"\n{lines}\n}}\n')
                self.assertEqual(depends_on_items(body), full)

    def test_the_modules_that_name_the_pool_by_string_are_found(self):
        # Guards the scan itself: an empty set would pass the check below for nothing.
        self.assertTrue({"kube-agents-iam", "github-minter"} <= modules_naming_the_pool_by_string())

    def test_every_such_module_waits_for_the_cluster(self):
        modules = modules_naming_the_pool_by_string()
        checked = 0
        for name, body in composition_modules(COMPOSITION.read_text()):
            source = module_source(body) or ""
            if not source.startswith(LOCAL_MODULE_PREFIX) or source[len(LOCAL_MODULE_PREFIX):] not in modules:
                continue
            checked += 1
            with self.subTest(module=name):
                items = depends_on_items(body)
                self.assertIsNotNone(items, f'module "{name}" has no depends_on')
                self.assertIn(CLUSTER_MODULE, items,
                              f'module "{name}" binds a Workload Identity member by string '
                              f"and does not wait for {CLUSTER_MODULE}")
        self.assertGreaterEqual(checked, 3)


if __name__ == "__main__":
    unittest.main()
