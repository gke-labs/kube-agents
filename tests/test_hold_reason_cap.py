"""The spec.hold reason cap is one number, kept by hand in five places.

The CRD admits a reason of up to ``maxLength`` characters; kube-proxy, the
credential broker, the cluster preflight and the bench helper each cut it to
their own copy of that number. A copy smaller than the CRD's cap would quote a
truncated reason to the agent, so every copy is read here and compared with the
generated CRD.
"""

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CRD_PATH = REPO_ROOT / "charts/kube-agents/crds/kubeagents.x-k8s.io_platformagents.yaml"
# Each copy of the cap: the file and a pattern whose one group is the number.
CAP_COPIES = {
    "k8s-api-proxy/pkg/k8sapiproxy/hold.go": r"holdReasonMaxRunes\s*=\s*(\d+)",
    "agents/platform/scripts/scope_exclude.py": r"^HOLD_REASON_MAX_LENGTH = (\d+)$",
    "agents/platform/scripts/cluster_preflight.sh": r"^readonly SCOPE_HOLD_REASON_MAX_CHARS=(\d+)$",
    "bench/kube_agents_bench/scope_hold.py": r"^MAX_REASON_CHARS = (\d+)$",
}


def crd_reason_max_length() -> int:
    crd = yaml.safe_load(CRD_PATH.read_text())
    lengths = {
        version["name"]: version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["hold"][
            "properties"
        ]["reason"]["maxLength"]
        for version in crd["spec"]["versions"]
    }
    assert len(set(lengths.values())) == 1, lengths
    return next(iter(lengths.values()))


class HoldReasonCapTest(unittest.TestCase):
    def test_every_copy_of_the_cap_matches_the_crd(self) -> None:
        want = crd_reason_max_length()
        for path, pattern in CAP_COPIES.items():
            with self.subTest(path=path):
                match = re.search(pattern, (REPO_ROOT / path).read_text(), re.MULTILINE)
                self.assertIsNotNone(match, f"{path} no longer declares the cap as {pattern}")
                self.assertEqual(want, int(match.group(1)))


if __name__ == "__main__":
    unittest.main()
