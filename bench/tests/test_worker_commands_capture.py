"""The harness captures each delegated worker's terminal commands before the purge.

Only the parse and the plumbing are testable without a cluster: ``_agent_shell``
is replaced by a canned worker log in the shape hermes writes, and the check is
that every ``💻 $`` line becomes one command row with its timing and exit
suffixes stripped, that ``_settle`` records them on the result, and that
``run()`` hands them to the stash.
"""

from __future__ import annotations

import pytest
from devops_bench.agents import AgentResult

from kube_agents_bench import harness, transcript

_LOG = """__WORKER_LOG__
  ┊ 💻 preparing terminal…
  ┊ 💻 $         python3 /opt/defaults/skills/version-control/scripts/vcs.py clone acme/infra  4.1s
  ┊ 🔎 preparing search_files…
  ┊ 💻 $         /opt/vcs/libexec/git log -3 --format='%h %s'  0.3s
  ┊ 💻 $         printenv  0.3s [exit 1]
  ┊ 💻 $         cd /tmp && gh api repos/acme/infra/commits  1.2s [exit 0]
  some unrelated line 12s
"""


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def test_every_command_line_becomes_a_row_without_its_suffixes(monkeypatch):
    seen = []

    def fake_shell(script, timeout):
        seen.append(script)
        return _LOG

    monkeypatch.setattr(harness, "_agent_shell", fake_shell)
    rows = harness._worker_commands(["t_ab12"], 5.0)
    assert [r["command"] for r in rows] == [
        "python3 /opt/defaults/skills/version-control/scripts/vcs.py clone acme/infra",
        "/opt/vcs/libexec/git log -3 --format='%h %s'",
        "printenv",
        "cd /tmp && gh api repos/acme/infra/commits",
    ]
    assert all(r["task"] == "t_ab12" for r in rows)
    assert seen and "/opt/data/kanban/logs/t_ab12.log" in seen[0]


def test_an_unreadable_log_is_none_so_the_check_errors_rather_than_grades(monkeypatch):
    # kubectl failed (a credential hiccup on the runner): no sentinel at all.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "")
    assert harness._worker_commands(["t_gone"], 5.0) is None


def test_an_absent_log_contributes_nothing_but_is_not_a_failure(monkeypatch):
    # The card never had a worker log (a router-only card): captured, empty.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "__NO_WORKER_LOG__\n")
    assert harness._worker_commands(["t_router"], 5.0) == []


def test_settle_records_the_commands_before_purging(monkeypatch):
    calls = []

    def fake_shell(script, timeout):
        calls.append(script)
        return _LOG if "head -c" in script and ".log" in script else ""  # rm -rf script gets ""

    monkeypatch.setattr(harness, "_agent_shell", fake_shell)
    result = AgentResult(output="answer", trajectory=[])
    result.metadata["final_message"] = "answer"
    harness.KubeAgentsHarness._settle(result, [], ["t_1"])
    rows = result.metadata["worker_commands"]
    assert [r["command"] for r in rows][0].endswith("vcs.py clone acme/infra")
    # The purge is the last agent-pod call, after the log was read.
    assert "rm -rf" in calls[-1]


def test_stash_carries_none_when_nothing_was_captured():
    transcript.set("x", [])
    assert transcript.get().worker_commands is None
    transcript.set("x", [], worker_commands=[])
    assert transcript.get().worker_commands == []
