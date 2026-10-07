"""The credential proxy's child memory budget terms, held equal across the
broker and the operator.

    python3 -m unittest tests.test_credential_proxy_sizing_parity

The broker (`agents/platform/scripts/credential_proxy.py`) admits requests
against these terms; the operator's sizing test
(`TestCredentialProxyOutputCapClearsTheLargestFleetDump` in
`k8s-operator/internal/controller/platformagent_manifests_test.go`) holds the
rendered memory limit to the floor they imply; and the chart
(`charts/kube-agents/templates/_helpers.tpl`) fails a render under that floor,
or crossing the operator's default requests and limits, from copies of its own.
Each side declares its own copy, so nothing but this test stops one moving
without the others. The files are read as text: importing the broker pulls in
its runtime dependencies.

The floor at the operator's output cap is declared as a literal on each side:
`CHILD_MEMORY_BUDGET_FLOOR_BYTES_AT_DEFAULT_CAP` in the broker,
`credentialProxyMemoryFloorBytesAtDefaultCap` in the operator, and
`kube-agents.credentialProxyMemoryFloorBytes` in the chart. This test holds the
three equal rather than re-deriving the floor, so a change to the shape of
either side's floor function is not hidden behind a formula of the test's own:
each side's own test (`test_a_limit_at_the_floor_enables_the_budget_for_two`,
`TestCredentialProxyBudgetArithmeticAtTheDefaults`) holds its literal to its
function.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BROKER = REPO_ROOT / "agents/platform/scripts/credential_proxy.py"
OPERATOR = REPO_ROOT / "k8s-operator/internal/controller/credential_proxy_manifests.go"
CHART_HELPERS = REPO_ROOT / "charts/kube-agents/templates/_helpers.tpl"
MEBIBYTE = 1024 * 1024

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


def broker_floor_bytes() -> int:
    text = BROKER.read_text(encoding="utf-8")
    return _match(r"^CHILD_MEMORY_BUDGET_FLOOR_BYTES_AT_DEFAULT_CAP = (\d+)$", text, BROKER)


def operator_floor_bytes() -> int:
    text = OPERATOR.read_text(encoding="utf-8")
    return _match(r"^\s*credentialProxyMemoryFloorBytesAtDefaultCap\s+int64\s*=\s*(\d+)\s*$", text, OPERATOR)


# (chart side, chart key, operator constant) for the defaults the chart merges over.
DEFAULTS = (
    ("requests", "cpu", "credentialProxyCPURequest"),
    ("requests", "memory", "credentialProxyMemoryRequest"),
    ("limits", "cpu", "credentialProxyCPULimit"),
    ("limits", "memory", "credentialProxyMemoryLimit"),
    ("limits", "ephemeral-storage", "credentialProxyEphemeralStorageLimit"),
)


def chart_floor_bytes() -> int:
    text = CHART_HELPERS.read_text(encoding="utf-8")
    return _match(
        r'^\{\{- define "kube-agents\.credentialProxyMemoryFloorBytes" -\}\}\n(\d+)\n\{\{- end \}\}$',
        text,
        CHART_HELPERS,
    )


def chart_defaults() -> dict[tuple[str, str], str]:
    text = CHART_HELPERS.read_text(encoding="utf-8")
    block = re.search(
        r'\{\{- define "kube-agents\.credentialProxyDefaults" -\}\}(.*?)\{\{- end \}\}', text, re.DOTALL
    )
    if block is None:
        raise AssertionError("_helpers.tpl declares no kube-agents.credentialProxyDefaults")
    defaults = {}
    for side in ("requests", "limits"):
        found = re.search(rf'"{side}" \(dict ([^)]*)\)', block.group(1))
        if found is None:
            raise AssertionError(f"kube-agents.credentialProxyDefaults carries no {side}")
        pairs = re.findall(r'"([^"]+)" "([^"]+)"', found.group(1))
        defaults.update({(side, key): value for key, value in pairs})
    return defaults


def operator_defaults() -> dict[str, str]:
    text = OPERATOR.read_text(encoding="utf-8")
    return {name: _quoted(rf'^\s*{name}\s*=\s*"([^"]+)"', text) for _, _, name in DEFAULTS}


def _quoted(pattern: str, text: str) -> str:
    found = re.search(pattern, text, re.MULTILINE)
    if found is None:
        raise AssertionError(f"{OPERATOR.relative_to(REPO_ROOT)} declares nothing matching {pattern!r}")
    return found.group(1)


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

    def test_the_three_declared_floors_are_equal(self):
        floors = {
            "CHILD_MEMORY_BUDGET_FLOOR_BYTES_AT_DEFAULT_CAP in credential_proxy.py": broker_floor_bytes(),
            "credentialProxyMemoryFloorBytesAtDefaultCap in credential_proxy_manifests.go": operator_floor_bytes(),
            "kube-agents.credentialProxyMemoryFloorBytes in _helpers.tpl": chart_floor_bytes(),
        }
        self.assertEqual(
            len(set(floors.values())),
            1,
            "the declared credential-proxy memory floors differ; change all three together: "
            + ", ".join(f"{where} = {value}" for where, value in floors.items()),
        )

    def test_the_chart_declares_the_operators_defaults(self):
        chart = chart_defaults()
        operator = operator_defaults()
        self.assertEqual(set(chart), {(side, key) for side, key, _ in DEFAULTS})
        for side, key, name in DEFAULTS:
            with self.subTest(default=f"{side}.{key}"):
                self.assertEqual(
                    chart[(side, key)],
                    operator[name],
                    f"kube-agents.credentialProxyDefaults {side}.{key} and {name} differ; change them together",
                )


if __name__ == "__main__":
    unittest.main()
