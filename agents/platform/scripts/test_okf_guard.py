"""Unit tests for deterministic OKF guard evaluation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import okf_guard


class OkfGuardOversizeAndDecoyTest(unittest.TestCase):
    """Pin oversize lockfile exemptions and multi-doc guard evaluation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_record(self, rel: str, content: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_oversize_lockfile_exemption_boundaries(self) -> None:
        self._write_record(
            "knowledge/decisions/DEC-2026-003-resource-scoped.md",
            "---\n"
            "type: decision\n"
            "id: DEC-2026-003\n"
            "title: Forbid hostNetwork on checkout\n"
            "status: active\n"
            "date: 2026-11-01\n"
            "scope:\n"
            "  resources:\n"
            "    - Deployment/*/checkout\n"
            "guard:\n"
            "  rule: hostNetwork must be absent.\n"
            "  severity: block\n"
            "  expect:\n"
            "    file: \"**/*.yaml\"\n"
            "    path: spec.template.spec.hostNetwork\n"
            "    op: absent\n"
            "---\n\nNever enable hostNetwork on checkout.\n",
        )

        out_lock_named = okf_guard.check_changed_files(
            self.root,
            {"clusters/prod/checkout-lock.yaml": None},
            oversize_files={"clusters/prod/checkout-lock.yaml"},
        )
        self.assertEqual(out_lock_named["verdict"], "block")
        self.assertEqual(out_lock_named["findings"][0]["verdict"], "violates")

        out_cluster_pnpm = okf_guard.check_changed_files(
            self.root,
            {"clusters/prod/pnpm-lock.yaml": None},
            oversize_files={"clusters/prod/pnpm-lock.yaml"},
        )
        self.assertEqual(out_cluster_pnpm["verdict"], "block")
        self.assertEqual(out_cluster_pnpm["findings"][0]["verdict"], "violates")

        out_web_pnpm = okf_guard.check_changed_files(
            self.root,
            {
                "web/pnpm-lock.yaml": None,
                "clusters/prod/checkout.yaml": (
                    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: checkout\nspec:\n"
                    "  template:\n    spec:\n      containers:\n      - name: checkout\n"
                    "        image: checkout:1.0\n"
                ),
            },
            oversize_files={"web/pnpm-lock.yaml"},
        )
        self.assertEqual(out_web_pnpm["verdict"], "ok")
        self.assertEqual(out_web_pnpm["findings"][0]["verdict"], "consistent")

    def test_oversize_lockfile_blocked_when_scope_paths_names_path(self) -> None:
        self._write_record(
            "knowledge/decisions/DEC-2026-004-web-scope.md",
            "---\n"
            "type: decision\n"
            "id: DEC-2026-004\n"
            "title: Guard web yaml manifests\n"
            "status: active\n"
            "date: 2026-11-01\n"
            "scope:\n"
            "  paths:\n"
            "    - web/**\n"
            "guard:\n"
            "  rule: hostNetwork must be absent.\n"
            "  severity: block\n"
            "  expect:\n"
            "    file: \"**/*.yaml\"\n"
            "    path: spec.template.spec.hostNetwork\n"
            "    op: absent\n"
            "---\n\nGuard web manifests.\n",
        )

        out = okf_guard.check_changed_files(
            self.root,
            {"web/pnpm-lock.yaml": None},
            oversize_files={"web/pnpm-lock.yaml"},
        )
        self.assertEqual(out["verdict"], "block")
        self.assertEqual(out["findings"][0]["verdict"], "violates")


if __name__ == "__main__":
    unittest.main()
