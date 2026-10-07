"""The credential proxy's child memory budget terms, held equal across the
broker and the operator.

    python3 -m unittest tests.test_credential_proxy_sizing_parity

The broker (`agents/platform/scripts/credential_proxy.py`) admits requests
against these terms; the operator's sizing test
(`TestCredentialProxyOutputCapClearsTheLargestFleetDump` in
`k8s-operator/internal/controller/platformagent_manifests_test.go`) holds the
rendered memory limit to the floor they imply. Each side declares its own copy,
so nothing but this test stops one moving without the other. Both files are
read as text: importing the broker pulls in its runtime dependencies.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BROKER = REPO_ROOT / "agents/platform/scripts/credential_proxy.py"
OPERATOR = REPO_ROOT / "k8s-operator/internal/controller/credential_proxy_manifests.go"
MEBIBYTE = 1024 * 1024
DEFAULT_OUTPUT_CAP_BYTES = 8 * MEBIBYTE
FLOOR_AT_DEFAULT_OUTPUT_CAP_BYTES = 672 * MEBIBYTE

# (broker name, operator name, whether the figure is in MiB on both sides).
TERMS = (
    ("BROKER_RESIDENT_RESERVE_BYTES", "credentialProxyResidentReserveBytes", True),
    ("CONTENT_WORKSPACE_RESERVE_BYTES", "credentialProxyWorkspaceReserveBytes", True),
    ("REQUEST_CHILD_MEMORY_RESERVE_BYTES", "credentialProxyRequestReserveBytes", True),
    ("OUTPUT_COPIES_PER_COMMAND", "credentialProxyOutputCopiesPerCommand", False),
    ("BUDGET_MINIMUM_ADMITTED_REQUESTS", "credentialProxyMinimumAdmittedRequests", False),
)


def _match(pattern: str, text: str, where: pathlib.Path) -> int:
    found = re.search(pattern, text, re.MULTILINE)
    if found is None:
        raise AssertionError(f"{where.relative_to(REPO_ROOT)} declares nothing matching {pattern!r}")
    return int(found.group(1))


def broker_terms() -> dict[str, int]:
    text = BROKER.read_text(encoding="utf-8")
    if not re.search(r"^MEBIBYTE = 1024 \* 1024$", text, re.MULTILINE):
        raise AssertionError("credential_proxy.py no longer declares MEBIBYTE = 1024 * 1024")
    terms = {}
    for name, _, in_mebibytes in TERMS:
        if in_mebibytes:
            terms[name] = _match(rf"^{name} = (\d+) \* MEBIBYTE$", text, BROKER) * MEBIBYTE
        else:
            terms[name] = _match(rf"^{name} = (\d+)$", text, BROKER)
    return terms


def operator_terms() -> dict[str, int]:
    text = OPERATOR.read_text(encoding="utf-8")
    terms = {}
    for _, name, in_mebibytes in TERMS:
        if in_mebibytes:
            terms[name] = _match(rf"^\s*{name}\s+int64\s*=\s*(\d+)\s*<<\s*20\s*$", text, OPERATOR) * MEBIBYTE
        else:
            terms[name] = _match(rf"^\s*{name}\s+int64\s*=\s*(\d+)\s*$", text, OPERATOR)
    return terms


def floor_bytes(resident: int, workspace: int, request: int, copies: int, minimum: int, cap: int) -> int:
    """The smallest limit that admits `minimum` slot-taking requests at once."""
    return resident + workspace + minimum * (request + copies * cap)


class CredentialProxySizingParityTest(unittest.TestCase):
    def test_each_term_is_declared_equal_on_both_sides(self):
        broker = broker_terms()
        operator = operator_terms()
        for broker_name, operator_name, _ in TERMS:
            with self.subTest(term=broker_name):
                self.assertEqual(
                    broker[broker_name],
                    operator[operator_name],
                    f"{broker_name} in credential_proxy.py and {operator_name} in "
                    "credential_proxy_manifests.go differ; change them together",
                )

    def test_both_sides_derive_the_same_floor_at_the_default_output_cap(self):
        broker = broker_terms()
        operator = operator_terms()
        broker_floor = floor_bytes(*(broker[name] for name, _, _ in TERMS), DEFAULT_OUTPUT_CAP_BYTES)
        operator_floor = floor_bytes(*(operator[name] for _, name, _ in TERMS), DEFAULT_OUTPUT_CAP_BYTES)
        self.assertEqual(FLOOR_AT_DEFAULT_OUTPUT_CAP_BYTES, broker_floor)
        self.assertEqual(FLOOR_AT_DEFAULT_OUTPUT_CAP_BYTES, operator_floor)


if __name__ == "__main__":
    unittest.main()
