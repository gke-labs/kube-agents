"""The platform persona's readiness run carries the declared scope.

`agents/platform/AGENTS.md` ("Delegation") answers a fleet-readiness question
with one run of `fleet_upgrade_report.py --readiness` and forbids the hand survey
that would replace it. Since the collectors sweep the install's declared scope,
that script refuses to run on an install that declares one unless it is handed
the `collector_args` the platform_control `fleet_scope` tool reports
(`agents/platform/scripts/fleet_scope_args.py`, `DeclaredScope.set`), and every
install the installer or the Terraform composition makes declares a scope. A
bullet that dictates the bare command therefore dictates a usage error there,
with the only alternative forbidden.

Nothing executes the prose, so these tests pin the slot: wherever a persona file
dictates the readiness run, the same `collector_args` placeholder the skill's own
run block uses stands beside it, and the bullet names the tool call.

Run:
  python3 -m unittest discover -s tests -p 'test_persona_readiness_route.py' -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_AGENTS = REPO_ROOT / "agents/platform/AGENTS.md"
READINESS_SKILL = REPO_ROOT / "agents/platform/skills/fleet-upgrade-verification/SKILL.md"
# Every persona file a Platform Agent worker reads; a run dictated in any of them is bound.
PERSONA_FILES = (
    PLATFORM_AGENTS,
    REPO_ROOT / "agents/platform/SOUL.md",
    REPO_ROOT / "agents/platform/CAPABILITIES.md",
)

REPORT_SCRIPT = "fleet_upgrade_report.py"
READINESS_FLAG = "--readiness"
BULLET_LEAD = "**Fleet versions and upgrade readiness**"
SCOPE_TOOL = "`fleet_scope`"
# The slot as the skill's run block and the audit SOPs spell it, so one placeholder names the
# step everywhere a collector is dictated.
COLLECTOR_ARGS_SLOT = "<collector_args from the fleet_scope tool, verbatim, or nothing on an install that declares no scope>"


def _dictated_runs(path: Path) -> list[str]:
    """The lines of ``path`` that dictate the readiness run: the script and its mode flag inside
    one backtick span on the line, which is how the persona writes a command to type."""
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        spans = line.split("`")[1::2]
        if any(REPORT_SCRIPT in span and READINESS_FLAG in span for span in spans):
            runs.append(line)
    return runs


class ReadinessRouteCarriesTheScopeTest(unittest.TestCase):
    def test_the_delegation_bullet_dictates_the_run(self):
        bullets = [line for line in _dictated_runs(PLATFORM_AGENTS) if BULLET_LEAD in line]
        self.assertEqual(len(bullets), 1, "the Delegation bullet dictates the readiness run once")

    def test_the_bullet_calls_fleet_scope_before_the_run(self):
        (bullet,) = [line for line in _dictated_runs(PLATFORM_AGENTS) if BULLET_LEAD in line]
        self.assertIn(SCOPE_TOOL, bullet)
        self.assertLess(bullet.index(SCOPE_TOOL), bullet.index(REPORT_SCRIPT), "the tool call comes first")

    def test_every_dictated_run_carries_the_collector_args_slot(self):
        for path in PERSONA_FILES:
            for line in _dictated_runs(path):
                with self.subTest(file=path.name):
                    command = next(span for span in line.split("`")[1::2] if REPORT_SCRIPT in span and READINESS_FLAG in span)
                    self.assertIn(COLLECTOR_ARGS_SLOT, command, f"{path.name} dictates the run without the declared scope: {command}")

    def test_the_slot_is_the_one_the_skill_spells(self):
        self.assertIn(COLLECTOR_ARGS_SLOT, READINESS_SKILL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
