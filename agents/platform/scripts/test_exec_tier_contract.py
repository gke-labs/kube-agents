"""The exec tier's shared names match the design's implementation contract.

The broker and the shim import these names from `credential_proxy_client`;
docs/designs/credential-broker-exec-tier.md §12 is what the operator, the chart
and the reviewers read. A name changed on one side and not the other is a
broker and a shim that disagree in production, so each value is checked in the
phrase of its own §12 row that gives it its meaning, not only somewhere in the
section, and the contract's bounds are the set the shim retries plus the ones
it does not.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import credential_proxy_client as client

DESIGN = Path(__file__).resolve().parents[3] / "docs" / "designs" / "credential-broker-exec-tier.md"


def _contract() -> str:
    text = DESIGN.read_text(encoding="utf-8")
    return text.split("## 12. Implementation contract", 1)[1]


def _row(item: str) -> str:
    """The Value cell of §12's row for ``item``."""
    (line,) = [line for line in _contract().splitlines() if line.startswith(f"| {item} ")]
    return line.split("|")[2].strip()


def _bounds(cell: str) -> set[str]:
    return set(re.findall(r"`([a-z-]+)`", cell))


class ExecTierContractTest(unittest.TestCase):
    def test_every_shared_name_is_in_its_row(self):
        for item, phrase in (
            ("Broker role", f"`CREDENTIAL_PROXY_ROLE={client.EXEC_ROLE}`."),
            ("Exec routes", f"`GET {client.API_RELAY_PREFIX}…`"),
            ("Caller URL", f"`{client.EXEC_URL_ENV}`, the exec Service's URL"),
            (
                "Replica count in pods",
                f"mounted at `{client.EXEC_REPLICAS_FILE}`; `{client.EXEC_REPLICAS_FILE_ENV}` names that path",
            ),
            ("Busy reply", f'"{client.BUSY_BOUND_KEY}": "<bound>"'),
            ("Busy reply", f"`{client.ADMISSION_BOUND_BACKGROUND}` is the background cap"),
            ("Busy reply", f"`{client.ADMISSION_BOUND_DRAINING}` is a replica that is stopping"),
            (
                "Classes",
                f"`{client.CLASS_INTERACTIVE}` and `{client.CLASS_BACKGROUND}`; "
                f"the metric label is `{client.CLASS_LABEL}`",
            ),
            (
                "Audit fields",
                f"`{client.AUDIT_BROKER_TIER}` (`{client.BROKER_TIER_EXEC}` or "
                f"`{client.BROKER_TIER_STATEFUL}`), `{client.AUDIT_CLASS}` and `{client.AUDIT_BROKER_POD}`",
            ),
        ):
            with self.subTest(item=item, phrase=phrase):
                self.assertIn(phrase, _row(item))

    def test_the_nice_value_is_the_contract_s(self):
        self.assertRegex(_row("Classes"), rf"\bnice {client.BACKGROUND_NICE}\b")

    def test_the_retried_bounds_are_the_contract_s(self):
        self.assertEqual(set(client.RETRYABLE_BUSY_BOUNDS), _bounds(_row("Shim retry")))
        bounds = _bounds(_row("Busy reply").split("The bounds are", 1)[1].split(";", 1)[0])
        self.assertTrue(client.RETRYABLE_BUSY_BOUNDS < bounds)


if __name__ == "__main__":
    unittest.main()
