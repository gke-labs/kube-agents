"""The web console reads every PlatformAgent status.usage field the gateway's feedback summary prints.

a2a/gateway/feedback_summary.go renders "/feedback summary" in chat from status.usage, and
GatewayCopyTest in agents/platform/scripts/test_feedback_summary.py holds its field lists to the
workstation script's. a2a/cmd/web-console/usage.go carries a third copy for the console's feedback
modal and banner, and copies only the fields it names off the status, so a field the gateway gains
reaches the console's report only once it is added there. These tests hold that copy to the
gateway's, with the same reads GatewayCopyTest makes: the lists by name, and the render's own
`usage[...]` reads, so a field the gateway starts printing without adding to a list fails here too.

Run:
  python3 -m unittest discover -s tests -p 'test_web_console_usage_parity.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_GO = REPO_ROOT / "a2a" / "gateway" / "feedback_summary.go"
CONSOLE_GO = REPO_ROOT / "a2a" / "cmd" / "web-console" / "usage.go"

# The gateway's list variables and the console's copies, by name.
LIST_PAIRS = (
    ("usageSummaryCounters", "usageCounters"),
    ("usageSummaryOriginFields", "usageOriginFields"),
    ("usageSummaryToolStatusOrder", "usageToolStatusOrder"),
)
# The readings map: the keys, and which suffix constant each key maps to.
READINGS_PAIR = ("usageSummaryReadings", "usageReadings")
READING_SUFFIXES = ("usageForgeReadingSuffix", "usageGatewayReadingSuffix")
# The gateway's render reads its three totals through these constants; the console lists its
# own three in usageTotalFields, in the same order.
TOTALS_RENDER_FUNC = "func renderUsageSummary("
TOTALS_RENDER_READ_RE = re.compile(r"usage\[(usageSummary\w+Field)\]")
CONSOLE_TOTALS_VAR = "usageTotalFields"
# The topTools constants both files name alike, and the render words both print.
SHARED_CONSTANTS = (
    "usageTopToolsKey",
    "usageToolField",
    "usageToolSubcommandField",
    "usageToolTotalField",
    "usageToolSubcommandNone",
    "usageToolPairSeparator",
    "usageTopToolsLabel",
    "usageForgeReadingSuffix",
    "usageGatewayReadingSuffix",
    "usageAgentReadingSuffix",
    "usageNoReading",
)
SHARED_INT_CONSTANTS = ("usageTopToolsShown",)
# The gateway's render words the console names differently (gateway name, console name).
RENAMED_CONSTANTS = (
    ("usageSummaryPerformanceHeader", "usagePerformanceHeader"),
    ("usageSummaryInferenceLabel", "usageInferenceLabel"),
    ("usageSummarySpendLabel", "usageSpendLabel"),
    ("usageSummarySpendCurrency", "usageSpendCurrency"),
    ("usageSummaryOriginLinePrefix", "usageOriginLinePrefix"),
    ("usageSummaryOriginFieldPrefix", "usageOriginFieldPrefix"),
    ("usageSummaryNever", "usageNever"),
    ("usageSummaryNone", "usageNone"),
    ("usageSummaryUnavailable", "usageUnavailable"),
)
RENAMED_NUMBERS = (("usageSummaryMicrosPerUSD", "usageMicrosPerUSD"),)
# Every function of the gateway file that reads status.usage by a literal key, and the
# console's lists that together are its allowlist.
GATEWAY_USAGE_READERS = ("func renderUsageSummary(", "func renderTopTools(", "func usageSummaryOriginLine(")
LITERAL_READ_RE = re.compile(r'usage\["([A-Za-z0-9]+)"\]')
CONSOLE_ALLOWLIST_VARS = ("usageCounters", "usageOriginFields", "usageTotalFields", "usageTimestampFields")
CONSOLE_ALLOWLIST_CONSTANTS = ("usageActiveInterfacesField", "usageTopToolsKey")

QUOTED_RE = re.compile(r'"([A-Za-z0-9]+)"')
# The render's format strings: the gateway's renderTopTools is the console's line for line, and
# the usage and performance lines of the gateway's render are among the console's.
FORMAT_RE = re.compile(r'fmt\.Sprintf\("([^"]*)"')
TOOLS_RENDER_FUNCS = ("func renderTopTools(", "func renderTopTools(")
USAGE_RENDER_FUNCS = (("func renderUsageSummary(", "func usageSummaryOriginLine("), ("func renderUsageLines(", "func usageOriginLine("))
# Lines the gateway's render spells as literals and the console names as constants.
LITERAL_LABELS = (('"  lastActive: "', "usageLastActiveLabel"), ('"  lastHumanActive: "', "usageLastHumanActiveLabel"))
IDENT_RE = re.compile(r"\b(usage\w+)\b")


def _source(path: Path) -> str:
    return path.read_text()


def _var_body(source: str, var: str) -> str:
    start = source.index(f"var {var} = ")
    return source[start : source.index("}", start)]


def _go_strings(source: str, var: str) -> list[str]:
    return QUOTED_RE.findall(_var_body(source, var))


def _func_body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start : source.index("\nfunc ", start + 1)]


def _constant(source: str, name: str) -> str:
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]*)"', source, re.M)
    if match is None:
        raise AssertionError(f"{name} is not a string constant")
    return match.group(1)


def _number(source: str, name: str) -> float:
    match = re.search(rf"^\s*{name}\s*=\s*([0-9_.]+)", source, re.M)
    if match is None:
        raise AssertionError(f"{name} is not a numeric constant")
    return float(match.group(1).replace("_", ""))


class ConsoleCopyTest(unittest.TestCase):
    """The console's copy of the gateway's status.usage lists is the gateway's, verbatim."""

    @classmethod
    def setUpClass(cls):
        cls.gateway = _source(GATEWAY_GO)
        cls.console = _source(CONSOLE_GO)

    def test_the_console_carries_the_same_lists(self):
        for gateway_var, console_var in LIST_PAIRS:
            with self.subTest(gateway_var):
                self.assertEqual(_go_strings(self.gateway, gateway_var), _go_strings(self.console, console_var))

    def test_the_console_names_the_same_readings_and_sources(self):
        gateway_var, console_var = READINGS_PAIR
        self.assertEqual(sorted(_go_strings(self.gateway, gateway_var)), sorted(_go_strings(self.console, console_var)))
        for suffix in READING_SUFFIXES:
            pattern = re.compile(rf'"([A-Za-z0-9]+)":\s*{suffix}')
            self.assertEqual(
                sorted(pattern.findall(_var_body(self.gateway, gateway_var))),
                sorted(pattern.findall(_var_body(self.console, console_var))),
                suffix,
            )

    def test_the_console_reads_the_same_totals_in_the_same_order(self):
        gateway_names = TOTALS_RENDER_READ_RE.findall(_func_body(self.gateway, TOTALS_RENDER_FUNC))
        gateway_values = [_constant(self.gateway, name) for name in gateway_names]
        console_names = [n for n in IDENT_RE.findall(_var_body(self.console, CONSOLE_TOTALS_VAR)) if n != CONSOLE_TOTALS_VAR]
        console_values = [_constant(self.console, name) for name in console_names]
        self.assertEqual(gateway_values, console_values)
        self.assertEqual(len(gateway_values), 3)

    def test_the_console_prints_the_same_words(self):
        for name in SHARED_CONSTANTS:
            with self.subTest(name):
                self.assertEqual(_constant(self.gateway, name), _constant(self.console, name))
        for name in SHARED_INT_CONSTANTS:
            with self.subTest(name):
                self.assertEqual(_number(self.gateway, name), _number(self.console, name))
        for gateway_name, console_name in RENAMED_CONSTANTS:
            with self.subTest(gateway_name):
                self.assertEqual(_constant(self.gateway, gateway_name), _constant(self.console, console_name))
        for gateway_name, console_name in RENAMED_NUMBERS:
            with self.subTest(gateway_name):
                self.assertEqual(_number(self.gateway, gateway_name), _number(self.console, console_name))

    def test_the_console_prints_the_same_line_shapes(self):
        gateway_tools, console_tools = TOOLS_RENDER_FUNCS
        self.assertEqual(
            FORMAT_RE.findall(_func_body(self.gateway, gateway_tools)),
            FORMAT_RE.findall(_func_body(self.console, console_tools)),
        )
        gateway_funcs, console_funcs = USAGE_RENDER_FUNCS
        gateway_formats = {f for sig in gateway_funcs for f in FORMAT_RE.findall(_func_body(self.gateway, sig))}
        console_formats = {f for sig in console_funcs for f in FORMAT_RE.findall(_func_body(self.console, sig))}
        self.assertEqual(sorted(console_formats - gateway_formats), [], "line shapes the console prints and the gateway does not")
        self.assertGreaterEqual(len(console_formats), 5)
        render = _func_body(self.gateway, "func renderUsageSummary(")
        for literal, console_name in LITERAL_LABELS:
            self.assertIn(literal, render)
            self.assertEqual(literal.strip('"'), _constant(self.console, console_name))

    def test_every_field_the_gateway_renders_is_on_the_console_allowlist(self):
        """A field the gateway's render reads by name, through a list, a constant or a literal,
        is one the console's decoder copies; a new one fails here until usage.go carries it."""
        rendered = set()
        for signature in GATEWAY_USAGE_READERS:
            body = _func_body(self.gateway, signature)
            rendered.update(LITERAL_READ_RE.findall(body))
            rendered.update(_constant(self.gateway, name) for name in TOTALS_RENDER_READ_RE.findall(body))
        for var in ("usageSummaryCounters", "usageSummaryOriginFields"):
            rendered.update(_go_strings(self.gateway, var))
        rendered.add(_constant(self.gateway, "usageTopToolsKey"))
        self.assertIn("lastHumanActiveTime", rendered)
        self.assertIn("activeInterfaces", rendered)
        self.assertIn("inputTokensTotal", rendered)

        allowed = set()
        for var in CONSOLE_ALLOWLIST_VARS:
            body = _var_body(self.console, var)
            allowed.update(QUOTED_RE.findall(body))
            allowed.update(_constant(self.console, n) for n in IDENT_RE.findall(body) if n != var and n.endswith("Field"))
        allowed.update(_constant(self.console, name) for name in CONSOLE_ALLOWLIST_CONSTANTS)
        self.assertEqual(sorted(rendered - allowed), [], "status.usage fields the gateway renders and the console does not read")

    def test_the_console_reads_only_what_the_gateway_renders(self):
        """The copy is not wider than the original: the console's lists name no field the gateway
        does not print, so the allowlist cannot drift into fields nobody audits for names."""
        gateway_fields = set(_go_strings(self.gateway, "usageSummaryCounters")) | set(_go_strings(self.gateway, "usageSummaryOriginFields"))
        gateway_fields.update(_constant(self.gateway, n) for n in TOTALS_RENDER_READ_RE.findall(_func_body(self.gateway, TOTALS_RENDER_FUNC)))
        for signature in GATEWAY_USAGE_READERS:
            gateway_fields.update(LITERAL_READ_RE.findall(_func_body(self.gateway, signature)))
        gateway_fields.add(_constant(self.gateway, "usageTopToolsKey"))
        console_fields = set()
        for var in CONSOLE_ALLOWLIST_VARS:
            body = _var_body(self.console, var)
            console_fields.update(QUOTED_RE.findall(body))
            console_fields.update(_constant(self.console, n) for n in IDENT_RE.findall(body) if n != var and n.endswith("Field"))
        console_fields.update(_constant(self.console, name) for name in CONSOLE_ALLOWLIST_CONSTANTS)
        self.assertEqual(sorted(console_fields - gateway_fields), [])


if __name__ == "__main__":
    unittest.main()
