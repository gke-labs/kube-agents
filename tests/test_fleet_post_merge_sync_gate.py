"""bench/tf/fleet's Config Sync and post-merge fixture are off unless a project is listed.

A project that is not in var.post_merge_sync_projects -- every pool project
before its pool steps, and every dev fleet -- must plan and apply cleanly
without the deploy key's secret, roles/gkehub.editor or the Config Management
API. So every block those need is counted by local.post_merge_sync_enabled,
the list defaults to empty, and no secret is read outside such a block.
"""

import pathlib
import re
import unittest

_FLEET = pathlib.Path(__file__).resolve().parents[1] / "bench" / "tf" / "fleet"
_GATE = "count = local.post_merge_sync_enabled ? 1 : 0"
# Files whose every resource and data block is gated, and gated blocks in
# files that hold ungated ones, by address.
_GATED_FILES = ("config-sync.tf", "post-merge-sync.tf")
_GATED_ADDRESSES = {
    "defects-a.tf": ("kubernetes_namespace_v1.seeded_post_merge", "kubernetes_deployment_v1.order_ledger"),
}
_BLOCK = re.compile(r'^(resource|data) "([a-z0-9_]+)" "([a-z0-9_]+)" \{\n((?:[ \t]*\n)*)([^\n]*)', re.MULTILINE)


def _blocks(name):
    """{address: first attribute line} for the file's top-level resource and data blocks."""
    path = _FLEET / name
    if not path.is_file():
        return None
    found = {}
    for kind, rtype, rname, _, first in _BLOCK.findall(path.read_text()):
        found[f"{'data.' if kind == 'data' else ''}{rtype}.{rname}"] = first.strip()
    return found


class PostMergeSyncGateTest(unittest.TestCase):
    def test_the_project_list_defaults_to_empty_and_drives_the_switch(self):
        variables = (_FLEET / "variables.tf").read_text()
        block = re.search(r'variable "post_merge_sync_projects" \{(.*?)\n\}', variables, re.DOTALL).group(1)
        self.assertRegex(block, r"default\s+= \[\]")
        self.assertIn(
            "post_merge_sync_enabled = contains(var.post_merge_sync_projects, var.project_id)",
            (_FLEET / "config-sync.tf").read_text(),
        )

    def test_every_block_that_needs_the_pool_steps_is_counted_by_the_switch(self):
        checked = 0
        for name in _GATED_FILES:
            blocks = _blocks(name)
            if blocks is None:
                continue
            for address, first in blocks.items():
                checked += 1
                self.assertEqual(first, _GATE, f"{name}: {address} is not gated by post_merge_sync_projects")
        for name, addresses in _GATED_ADDRESSES.items():
            blocks = _blocks(name) or {}
            for address in addresses:
                if address in blocks:
                    checked += 1
                    self.assertEqual(blocks[address], _GATE, f"{name}: {address} is not gated")
        self.assertGreater(checked, 0)

    def test_no_secret_is_read_outside_the_switch(self):
        for path in sorted(_FLEET.glob("*.tf")):
            for address, first in (_blocks(path.name) or {}).items():
                if address.startswith("data.google_secret_manager"):
                    self.assertEqual(first, _GATE, f"{path.name}: {address} reads a secret on every plan")


if __name__ == "__main__":
    unittest.main()
