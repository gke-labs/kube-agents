"""A Workload Identity binding named by string waits for the cluster that creates its pool.

    python3 -m unittest tests.test_workload_identity_waits_for_cluster

A module that builds its binding's member as `serviceAccount:${var.project_id}.svc.id.goog[...]`
gives Terraform no edge to the cluster. The pool `<project>.svc.id.goog` exists only once the
project's first Workload-Identity-enabled cluster does, so on a first install into a new project
such a binding, created in parallel with the cluster, fails with "Identity Pool does not exist".
Every instantiation of such a module in the composition therefore names `module.gke_cluster` in
its `depends_on`.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULES = REPO_ROOT / "terraform" / "modules"
COMPOSITION = REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
# A member string that names the pool by interpolation rather than by a cluster reference.
POOL_BY_STRING = re.compile(r'"serviceAccount:\$\{[^}]+\}\.svc\.id\.goog\[')
MODULE_BLOCK = re.compile(r'^module "(?P<name>[^"]+)" \{\n(?P<body>.*?)^\}', re.MULTILINE | re.DOTALL)
SOURCE = re.compile(r'^\s*source\s*=\s*"\.\./\.\./modules/(?P<module>[^"]+)"', re.MULTILINE)
# The list holds characters other than brackets, or a whole `[...]` index, so an item such as
# `module.x[0]` does not end it, on one line or several; the first unmatched `]` does.
DEPENDS_ON = re.compile(r"^\s*depends_on\s*=\s*\[(?P<items>(?:[^\[\]]|\[[^\]]*\])*)\]", re.MULTILINE)
CLUSTER_MODULE = "module.gke_cluster"


def modules_naming_the_pool_by_string() -> set[str]:
    return {path.parent.name for path in MODULES.glob("*/*.tf") if POOL_BY_STRING.search(path.read_text())}


def depends_on_items(items: str) -> set[str]:
    return {item.strip() for item in items.split(",") if item.strip()}


class WorkloadIdentityWaitsForClusterTest(unittest.TestCase):
    def test_a_depends_on_list_is_read_whole(self):
        for body in (
            "  depends_on = [google_project_service.required, module.drift_pubsub[0], module.gke_cluster]\n",
            "  depends_on = [\n    google_project_service.required,\n    module.drift_pubsub[0]\n"
            "    , module.gke_cluster\n  ]\n",
            "  depends_on = [\n    module.drift_pubsub[0],\n    module.gke_cluster,\n  ]\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    depends_on_items(DEPENDS_ON.search(body)["items"]),
                    {"google_project_service.required", "module.drift_pubsub[0]", "module.gke_cluster"}
                    if "google_project_service" in body else {"module.drift_pubsub[0]", "module.gke_cluster"},
                )

    def test_the_modules_that_name_the_pool_by_string_are_found(self):
        # Guards the scan itself: an empty set would pass the check below for nothing.
        self.assertTrue({"kube-agents-iam", "github-minter"} <= modules_naming_the_pool_by_string())

    def test_every_such_module_waits_for_the_cluster(self):
        modules = modules_naming_the_pool_by_string()
        checked = 0
        for block in MODULE_BLOCK.finditer(COMPOSITION.read_text()):
            source = SOURCE.search(block["body"])
            if source is None or source["module"] not in modules:
                continue
            checked += 1
            with self.subTest(module=block["name"]):
                depends = DEPENDS_ON.search(block["body"])
                self.assertIsNotNone(depends, f'module "{block["name"]}" has no depends_on')
                self.assertIn(CLUSTER_MODULE, depends_on_items(depends["items"]),
                              f'module "{block["name"]}" binds a Workload Identity member by string '
                              f"and does not wait for {CLUSTER_MODULE}")
        self.assertGreaterEqual(checked, 3)


if __name__ == "__main__":
    unittest.main()
