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

from markdown_it import MarkdownIt

from test_skill_inline_commands import inline_code

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
OUTPUT_FLAG = "--output"
# What makes the dictated output name this run's own: a timestamp and the shell's PID, since a
# timestamp alone is second-resolution and two cards started in one second would share the name.
PER_RUN_NAME_MARKS = ("$(date +%s)", "$$")
BULLET_MARKER = "- "


def _fenced_lines(text: str):
    """(line number, line) for every line inside a fenced code block of ``text``, the other
    form a persona file uses for a command to type."""
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "fence":
            for offset, line in enumerate(token.content.splitlines()):
                yield token.map[0] + 2 + offset, line


def _dictates_the_run(text: str) -> bool:
    return REPORT_SCRIPT in text and READINESS_FLAG in text


def _dictated_runs(text: str) -> list[tuple[int, str]]:
    """The commands in ``text`` that dictate the readiness run, each with the line it opens on:
    the script and its mode flag inside one inline code span, or on one line of a fenced block,
    the two forms the persona writes a command to type in. Read with the Markdown parser the
    skill tests use (``test_skill_inline_commands.inline_code``), so a span written with double
    backticks or wrapped onto a second line is one span, as a reader sees it."""
    runs = [(line, span) for line, span in inline_code(text) if _dictates_the_run(span)]
    runs += [(line, fenced) for line, fenced in _fenced_lines(text) if _dictates_the_run(fenced)]
    return sorted(runs)


def _bullet_holding(text: str, line_number: int) -> str:
    """The list item that holds ``line_number``, from its marker to that line, as one string."""
    lines = text.splitlines()
    start = line_number - 1
    while start > 0 and not lines[start].lstrip().startswith(BULLET_MARKER):
        start -= 1
    return " ".join(line.strip() for line in lines[start:line_number])


def _delegation_bullet_runs() -> list[tuple[str, str]]:
    """(bullet, span) for each dictated run inside the Delegation bullet."""
    text = PLATFORM_AGENTS.read_text(encoding="utf-8")
    runs = []
    for line, span in _dictated_runs(text):
        bullet = _bullet_holding(text, line)
        if BULLET_LEAD in bullet:
            runs.append((bullet, span))
    return runs


class ReadinessRouteCarriesTheScopeTest(unittest.TestCase):
    def test_the_delegation_bullet_dictates_the_run(self):
        self.assertEqual(len(_delegation_bullet_runs()), 1, "the Delegation bullet dictates the readiness run once")

    def test_the_bullet_calls_fleet_scope_before_the_run(self):
        ((bullet, _),) = _delegation_bullet_runs()
        self.assertIn(SCOPE_TOOL, bullet)
        self.assertLess(bullet.index(SCOPE_TOOL), bullet.index(REPORT_SCRIPT), "the tool call comes first")

    def test_every_dictated_run_carries_the_collector_args_slot(self):
        for path in PERSONA_FILES:
            for line, command in _dictated_runs(path.read_text(encoding="utf-8")):
                with self.subTest(file=path.name, line=line):
                    self.assertIn(COLLECTOR_ARGS_SLOT, command, f"{path.name} dictates the run without the declared scope: {command}")

    def test_the_reader_sees_a_double_backtick_a_wrapped_span_and_a_fenced_line(self):
        # The guard above holds only if a dictated run reaches it however it is spelled: a
        # double-backtick span, a span wrapped onto the next line and a line of a fenced block
        # are each one dictated run, at its own line.
        text = (
            f"- Lead: run ``python3 x/{REPORT_SCRIPT} {READINESS_FLAG}`` now.\n"
            f"- Lead: run `python3 x/{REPORT_SCRIPT}\n  {READINESS_FLAG}` now, and `{REPORT_SCRIPT}` alone.\n"
            f"\n```bash\necho first\npython3 x/{REPORT_SCRIPT} {READINESS_FLAG} --output r.json\n```\n"
        )
        self.assertEqual(
            [(1, f"python3 x/{REPORT_SCRIPT} {READINESS_FLAG}"), (2, f"python3 x/{REPORT_SCRIPT} {READINESS_FLAG}"), (7, f"python3 x/{REPORT_SCRIPT} {READINESS_FLAG} --output r.json")],
            _dictated_runs(text),
        )

    def test_every_line_naming_the_run_is_a_dictated_run_the_reader_yields(self):
        # A dictation the reader does not yield passes the slot guard unread: the script in one
        # span and the flag in the next, or the two in prose. Every persona line that names both
        # must be one the reader yields, so such an edit fails here rather than shipping.
        for path in PERSONA_FILES:
            text = path.read_text(encoding="utf-8")
            yielded = {line for line, _ in _dictated_runs(text)}
            for number, line in enumerate(text.splitlines(), 1):
                if _dictates_the_run(line):
                    with self.subTest(file=path.name, line=number):
                        self.assertIn(number, yielded, f"{path.name}:{number} names the run outside a span the reader yields: {line.strip()[:80]}")
        split = f"- Lead: run `{REPORT_SCRIPT}` with `{READINESS_FLAG}`.\n"
        self.assertEqual([], _dictated_runs(split))
        self.assertTrue(_dictates_the_run(split))

    def test_the_output_path_is_this_runs_own(self):
        # /opt/data/scratch is shared by every card, so a fixed --output name is two cards writing
        # one file; the dictated command names the file for this run alone.
        ((_, command),) = _delegation_bullet_runs()
        # The name is the rest of the dictated command: a `$(date +%s)` inside it holds a space.
        output = command.split(OUTPUT_FLAG, 1)[1].strip()
        for mark in PER_RUN_NAME_MARKS:
            with self.subTest(mark=mark):
                self.assertIn(mark, output, f"the --output name is not this run's own: {output}")

    def test_the_slot_is_the_one_the_skill_spells(self):
        self.assertIn(COLLECTOR_ARGS_SLOT, READINESS_SKILL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
