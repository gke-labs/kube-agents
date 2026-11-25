"""seeded-c's default pool: its kubelet plant and the room its node keeps.

The node is an e2-standard-2. On an e2-medium (940m allocatable, as every
shared-core E2 size) the pool had about 60m of CPU to spare, and a node
replacement could leave kube-dns at two replicas on the one node with the
slot's fixture pods Pending for good.

`bench/tf/fleet/main.tf`'s `seeded_c_default` is the pool-level drift outlier
(fixture role `drift-pool-outlier`): a 50 GB boot disk and one kubelet key no
peer pool sets. The key is `pod_pids_limit`, an integer, because the pinned
Google provider sends a non-zero integer on a node-pool update as on a create.
The explicit `cpu_cfs_quota = false` the block first carried was sent only at
pool creation: on a pool that already existed the provider planned nothing,
and the role read drifted on every project older than the block.
"""

import pathlib
import re
import unittest

_MAIN_TF = (
    pathlib.Path(__file__).resolve().parents[1] / "bench" / "tf" / "fleet" / "main.tf"
)
_POOL_HEAD = 'resource "google_container_node_pool" "seeded_c_default" {'
# What bench/tf/fleet/fixtures.json's drift-pool-outlier role asserts.
_PLANTED_PIDS_LIMIT = "4096"
# What fixtures.json's untainted-compute-class-pool role asserts.
_MACHINE_TYPE = '"e2-standard-2"'


def _pool_block() -> str:
    text = _MAIN_TF.read_text(encoding="utf-8")
    start = text.index(_POOL_HEAD)
    end = text.index("\n}\n", start)
    # Comments name the earlier key too; only the HCL is read.
    return "\n".join(
        line
        for line in text[start:end].splitlines()
        if not line.lstrip().startswith("#")
    )


def _nested(block: str, name: str) -> str:
    # The closing brace at the block's own indentation, so a nested block's
    # close does not end the outer one early.
    match = re.search(
        rf"^([ \t]*){name}\s*\{{(.*?)^\1\}}", block, re.MULTILINE | re.DOTALL
    )
    if match is None:
        raise AssertionError(f"seeded_c_default has no {name} block")
    return match.group(2)


def _settings(block: str) -> dict[str, str]:
    return dict(re.findall(r"^\s*(\w+)\s*=\s*(\S+)", block, re.MULTILINE))


class SeededCDefaultPoolTest(unittest.TestCase):
    def test_the_node_is_a_dedicated_core_size_with_room_for_a_second_kube_dns(self):
        node = _settings(_nested(_pool_block(), "node_config"))
        self.assertEqual(
            node.get("machine_type"),
            _MACHINE_TYPE,
            "every shared-core E2 size allocates 940m, which a second kube-dns replica fills after a node replacement; fixtures.json's untainted-compute-class-pool asserts this size",
        )

    def test_the_kubelet_plant_is_an_integer_the_provider_sends_on_update(self):
        kubelet = _settings(
            _nested(_nested(_pool_block(), "node_config"), "kubelet_config")
        )
        self.assertEqual(
            kubelet.get("pod_pids_limit"),
            _PLANTED_PIDS_LIMIT,
            "fixtures.json's drift-pool-outlier asserts kubeletConfig.podPidsLimit eq 4096",
        )

    def test_no_explicit_false_the_provider_cannot_apply_to_an_existing_pool(self):
        kubelet = _settings(
            _nested(_nested(_pool_block(), "node_config"), "kubelet_config")
        )
        self.assertNotIn(
            "cpu_cfs_quota",
            kubelet,
            "the pinned provider sends cpu_cfs_quota = false only when it creates a pool",
        )


if __name__ == "__main__":
    unittest.main()
