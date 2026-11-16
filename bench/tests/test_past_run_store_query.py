"""The report-store objective of ``fleet-audit-reports-past-run`` reads the
query however the worker typed it.

Every fixture is graded through ``WorkerCommandsVerifier.verify()`` on a
staged transcript, as the run grades it: the card log's rendered row and the
tagged call as typed, together. The rendered rows are hand-written copies of
what Hermes's ``summarize_shell_command`` (``agent/display.py`` in the pinned
image) prints for each typed command, checked against that code: a chain is
its first segment plus `` + N command(s)``, where an ``echo`` of an exit code
is not counted, a redirect is dropped, and a newline is a space. The nightly
answers of 2026-10-20 to 2026-10-23 all assigned the script's path to a shell
variable and ran the variable, so their rows showed the assignment only and
the check failed a correct route.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "fleet-audit-reports-past-run" / "task.yaml"
OBJECTIVE = "the-store-was-queried"
CARD = "t_1"
SCRIPT = "/opt/data/profiles/platform/skills/fleet-audit-reports/scripts/report_query.py"
TIMEOUT_SEC = 5

# (typed command, the card log's rendering of it). A row equal to the typed
# command is a one-line command with no chain, which renders to itself.
QUERIES = [
    # The four recorded shapes: the path in a variable, then the variable run.
    (
        f'Q={SCRIPT}; python3 $Q streams; echo "EXIT $?"; python3 $Q show compliance-audit; echo "EXIT $?"',
        f"Q={SCRIPT} + 2 commands",
    ),
    (
        f"S={SCRIPT}; cd /tmp; python3 $S findings compliance-audit > /tmp/new.json; python3 $S runs compliance-audit | head -c 1500",
        f"S={SCRIPT} + 2 commands",
    ),
    (
        f"cd /tmp; Q={SCRIPT}; python3 $Q findings compliance-audit > a.json",
        f"Q={SCRIPT} + 1 command",
    ),
    (
        f"Q={SCRIPT}; for r in 20261023T004910Z 20261023T011208Z; do python3 $Q show compliance-audit --run $r | grep status; done",
        f"Q={SCRIPT} + 3 commands",
    ),
    # The variable braced, quoted, and run on a later line.
    (f'Q={SCRIPT}; python3 "${{Q}}" diff compliance-audit', f"Q={SCRIPT} + 1 command"),
    (f"Q={SCRIPT} && python3 -u ${{Q}} --root /tmp/r checks compliance-audit", f"Q={SCRIPT} + 1 command"),
    (f"Q={SCRIPT}\npython3 $Q runs compliance-audit", f"Q={SCRIPT} python3 $Q runs compliance-audit"),
    # The variable behind a wrapper, run bare (the script is executable),
    # and inside a substitution.
    (f"Q={SCRIPT}; timeout 60 python3 $Q streams", f"Q={SCRIPT} + 1 command"),
    (f"Q={SCRIPT}; $Q show compliance-audit", f"Q={SCRIPT} + 1 command"),
    (f"Q={SCRIPT}; OUT=`python3 $Q streams`; echo $OUT", f"Q={SCRIPT} + 2 commands"),
    # The direct forms the case always read.
    ("python3 ./skills/fleet-audit-reports/scripts/report_query.py streams",) * 2,
    (f"python3 {SCRIPT} diff compliance-audit",) * 2,
    ("python3 scripts/report_query.py --root /tmp/r show compliance-audit",) * 2,
    ("cd scripts && python3 report_query.py findings compliance-audit", "python3 report_query.py findings compliance-audit"),
    ("python -u report_query.py runs compliance-audit",) * 2,
]

# Commands that name the script and query nothing.
NOT_QUERIES = [
    ("grep -n 'report_query.py show' ./skills/fleet-audit-reports/SKILL.md",) * 2,
    ('echo "report_query.py streams"',) * 2,
    ("cat ./skills/fleet-audit-reports/SKILL.md",) * 2,
    # The path assigned and never run.
    (f"Q={SCRIPT}; ls -l $Q; echo streams", f"Q={SCRIPT} + 2 commands"),
    # Another variable run in the script's place.
    (f"Q={SCRIPT}; R=/opt/other/tool.py; python3 $R streams", f"Q={SCRIPT} + 2 commands"),
    (f"Q={SCRIPT}; QQ=/opt/other/tool.py; python3 $QQ streams", f"Q={SCRIPT} + 2 commands"),
    # The script's usage printed, no subcommand.
    (f"Q={SCRIPT}; python3 $Q", f"Q={SCRIPT} + 1 command"),
]


def _check() -> dict:
    spec = yaml.safe_load(TASK.read_text())
    entries = [e for e in spec["verification_spec"] if e["name"] == OBJECTIVE]
    assert len(entries) == 1, f"{OBJECTIVE} is not in {TASK}"
    return entries[0]["check"]


def _call(command: str) -> dict:
    """One tagged terminal call of the worker card, as worker_trajectory records it."""
    return {
        "name": "terminal",
        "args": {"command": command},
        "result": "",
        "status": "completed",
        "agent": "platform",
        "task": CARD,
        "session": "s_1",
        "at": 100.0,
    }


def _passes(typed: str, row: str) -> bool:
    assert "\n" not in row, row
    transcript.set(
        output="",
        trajectory=[_call(typed)],
        worker_commands=[{"task": CARD, "command": row}],
    )
    try:
        result = WorkerCommandsVerifier(**_check()).verify(timeout_sec=TIMEOUT_SEC)
    finally:
        transcript.clear()
    assert result.status != "error", result.reason
    return result.success


@pytest.mark.parametrize("typed,row", QUERIES)
def test_a_store_query_passes_however_it_was_typed(typed, row):
    assert _passes(typed, row), typed


@pytest.mark.parametrize("typed,row", NOT_QUERIES)
def test_a_command_that_only_names_the_script_fails(typed, row):
    assert not _passes(typed, row), typed
