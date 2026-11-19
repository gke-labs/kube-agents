"""seeded-d keeps the Filestore CSI driver off.

`bench/tf/fleet/main.tf`'s `seeded_d` cluster carries the zonal-skew fixtures,
four 16Mi pods pinned to the first zone's e2-small. GKE turns the Filestore
CSI driver on by default on clusters created on 1.36.3 or later, and its node
agent and lock-release controller take about 200Mi of that node, which left
the fixtures Pending on six of the ten pool projects created on 2026-11-16.
Nothing in the fleet mounts Filestore. Without the block the fleet reconcile
turns the driver back on, and the mistake shows only an hour later as
`zonal-skew-volume` and `zonal-skew-scheduling` drift on the fixture scan.
"""

import pathlib
import re
import unittest

_MAIN_TF = (
    pathlib.Path(__file__).resolve().parents[1] / "bench" / "tf" / "fleet" / "main.tf"
)
_CLUSTER_HEAD = 'resource "google_container_cluster" "seeded_d" {'


def _cluster_block() -> str:
    text = _MAIN_TF.read_text(encoding="utf-8")
    start = text.index(_CLUSTER_HEAD)
    end = text.index("\n}\n", start)
    # Comments name the add-on too; only the HCL is read.
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
        raise AssertionError(f"seeded_d has no {name} block")
    return match.group(2)


def _setting(block: str, name: str) -> str:
    match = re.search(rf"^\s*{name}\s*=\s*(\S+)", block, re.MULTILINE)
    if match is None:
        raise AssertionError(f"seeded_d sets no {name}")
    return match.group(1)


class SeededDClusterTest(unittest.TestCase):
    def test_the_filestore_csi_driver_is_off_so_the_zone_pinned_fixtures_fit(self):
        addons = _nested(_cluster_block(), "addons_config")
        filestore = _nested(addons, "gcp_filestore_csi_driver_config")
        self.assertEqual(
            _setting(filestore, "enabled"),
            "false",
            "the driver's two pods take about 200Mi of the first zone's e2-small, which the four 16Mi zone-pinned fixtures need",
        )


if __name__ == "__main__":
    unittest.main()
