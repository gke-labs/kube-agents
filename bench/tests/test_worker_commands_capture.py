"""The harness captures each delegated worker's terminal commands before the purge.

Only the parse and the plumbing are testable without a cluster: ``_agent_shell``
is replaced by a canned worker log in the shape hermes writes, and the check is
that every ``💻 $`` line becomes one command row with its timing and exit
suffixes stripped, that ``_settle`` records them on the result, and that
``run()`` hands them to the stash.
"""

from __future__ import annotations

import subprocess
import sys

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
    rows = harness._worker_commands(harness._worker_logs(["t_ab12"], 5.0))
    assert [r["command"] for r in rows] == [
        "python3 /opt/defaults/skills/version-control/scripts/vcs.py clone acme/infra",
        "/opt/vcs/libexec/git log -3 --format='%h %s'",
        "printenv",
        "cd /tmp && gh api repos/acme/infra/commits",
    ]
    assert all(r["task"] == "t_ab12" for r in rows)
    assert seen and "/opt/data/kanban/logs/t_ab12.log" in seen[0]


def test_the_read_returns_text_when_the_output_is_cut_inside_a_glyph(monkeypatch):
    # ``head -c`` can cut a transcript between the bytes of one character. The
    # decode must not raise out of _settle ahead of the purge that follows it,
    # so this runs the real subprocess call, against a child that writes a
    # truncated multibyte sequence in kubectl's place.
    real_run = subprocess.run

    def run_a_truncated_writer(cmd, **kwargs):
        assert cmd[:2] == ["kubectl", "exec"]
        return real_run(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xe2\\x94')"],
            **kwargs,
        )

    monkeypatch.setattr(harness.subprocess, "run", run_a_truncated_writer)
    assert harness._agent_shell("head -c 5 /opt/data/kanban/logs/t_1.log", 10.0).startswith("ok ")


def test_an_unreadable_log_is_none_so_the_check_errors_rather_than_grades(monkeypatch):
    # kubectl failed (a credential hiccup on the runner): no sentinel at all.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "")
    logs = harness._worker_logs(["t_gone"], 5.0)
    assert logs == {"t_gone": None}
    assert harness._worker_commands(logs) is None


def test_one_unread_card_keeps_the_transcripts_read_around_it(monkeypatch, tmp_path):
    # Review finding: the first unread card returned None for the whole map,
    # so a transcript already in hand was dumped as "unread" and the cards
    # after it were never read. The verifier still errors on the partial
    # capture; the dump keeps what was read.
    def fake_shell(script, timeout):
        if "t_read" in script:
            return "__WORKER_LOG__ 1758579012 83\nbanner\n"
        if "t_after" in script:
            return "__NO_WORKER_LOG__\n"
        return ""  # t_lost: the exec failed

    monkeypatch.setattr(harness, "_agent_shell", fake_shell)
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    logs = harness._worker_logs(["t_read", "t_lost", "t_after"], 5.0)
    assert set(logs) == {"t_read", "t_lost"} and logs["t_lost"] is None
    assert harness._worker_commands(logs) is None
    harness._dump_worker_logs(logs, ["t_read", "t_lost", "t_after"])
    index = (tmp_path / "worker-logs" / "index.txt").read_text().splitlines()
    assert index[1:] == [
        "t_read\t1758579012\t83\t__WORKER_LOG__",
        "t_lost\t-\t-\t__WORKER_LOG_UNREAD__",
        "t_after\t-\t-\t__NO_WORKER_LOG__",
    ]
    assert (tmp_path / "worker-logs" / "t_read.log").read_text() == "banner\n"
    assert not (tmp_path / "worker-logs" / "t_lost.log").exists()


def test_no_delegated_card_means_nothing_captured_not_an_empty_capture(monkeypatch):
    # Review finding: a run that delegated nothing reached _settle with no
    # task ids and recorded [], which graded as "0 commands" -- a forbidden-only
    # check passed on it. None is what the verifier reports as status=error.
    calls = []
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: calls.append(script) or "")
    assert harness._worker_logs([], 5.0) is None
    assert calls == []
    result = AgentResult(output="42", trajectory=[])
    result.metadata["final_message"] = "42"
    harness.KubeAgentsHarness._settle(result, [], [])
    assert result.metadata["worker_commands"] is None


def test_an_absent_log_contributes_nothing_but_is_not_a_failure(monkeypatch):
    # The card never had a worker log (a router-only card): captured, empty.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "__NO_WORKER_LOG__\n")
    assert harness._worker_logs(["t_router"], 5.0) == {}
    assert harness._worker_commands({}) == []


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


def test_a_stalled_card_is_archived_after_its_transcript_is_read_and_before_the_purge(
    monkeypatch, caplog
):
    # The unit gave up on the card; its worker would otherwise keep its
    # dispatcher slot until it finished on its own.
    calls = []

    def fake_shell(script, timeout):
        calls.append(script)
        if "kanban archive" in script:
            return "Archived t_2\n"
        return _LOG if "head -c" in script and ".log" in script else ""

    monkeypatch.setattr(harness, "_agent_shell", fake_shell)
    result = AgentResult(output="answer", trajectory=[])
    result.metadata["final_message"] = "answer"
    with caplog.at_level("INFO", logger="kube_agents_bench.harness"):
        harness.KubeAgentsHarness._settle(result, [], ["t_1", "t_2"], stalled=["t_2"])
    archive = [i for i, s in enumerate(calls) if "kanban archive" in s]
    assert len(archive) == 1
    assert "'t_2'" in calls[archive[0]] and "'t_1'" not in calls[archive[0]]
    read = next(i for i, s in enumerate(calls) if "head -c" in s)
    assert read < archive[0] < len(calls) - 1
    assert "rm -rf" in calls[-1]
    assert "archived stalled card t_2" in caplog.text
    assert "could not archive" not in caplog.text


def test_each_stalled_card_is_archived_in_its_own_exec(monkeypatch):
    # hermes exits 1 if any id in a batch fails and the exec then returns
    # nothing at all, so one card the dispatcher already archived would hide
    # whether the others were stopped.
    calls = []
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: calls.append(script) or "")
    harness._archive_stalled_cards(["t_a", "t_b"], 5.0)
    assert ["'t_a'" in s for s in calls] == [True, False]
    assert ["'t_b'" in s for s in calls] == [False, True]


def test_an_archive_that_fails_is_a_warning_naming_the_card(monkeypatch, caplog):
    # _agent_shell returns "" for a failed exec exactly as for silence; the
    # log must not read as if the worker was stopped.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "")
    with caplog.at_level("WARNING", logger="kube_agents_bench.harness"):
        harness._archive_stalled_cards(["t_stuck"], 5.0)
    assert "could not archive stalled card t_stuck" in caplog.text


def test_a_refused_archive_warns_with_hermes_reason(monkeypatch, caplog):
    # Review finding: hermes prints its refusal on stderr and exits 1, which
    # _agent_shell turns into "", so the warning always read "no output". The
    # script folds stderr in and exits 0 so the reason reaches the log.
    scripts = []

    def fake_shell(script, timeout):
        scripts.append(script)
        return "Error: task t_gone not found\n"

    monkeypatch.setattr(harness, "_agent_shell", fake_shell)
    with caplog.at_level("WARNING", logger="kube_agents_bench.harness"):
        harness._archive_stalled_cards(["t_gone"], 5.0)
    assert scripts[0].endswith("2>&1 || true")
    assert "could not archive stalled card t_gone" in caplog.text
    assert "task t_gone not found" in caplog.text


def test_a_card_that_settled_is_not_archived(monkeypatch):
    calls = []
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: calls.append(script) or "")
    result = AgentResult(output="answer", trajectory=[])
    result.metadata["final_message"] = "answer"
    harness.KubeAgentsHarness._settle(result, [], ["t_1"])
    assert not any("kanban archive" in s for s in calls)


def test_one_read_serves_both_the_verifier_and_the_dump(monkeypatch, tmp_path):
    # Each read is a kubectl exec into the agent pod, so the log is fetched
    # once per card however many consumers it has.
    reads = []

    def fake_shell(script, timeout):
        if "head -c" in script and ".log" in script:
            reads.append(script)
            return _LOG
        return ""

    monkeypatch.setattr(harness, "_agent_shell", fake_shell)
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    result = AgentResult(output="answer", trajectory=[])
    result.metadata["final_message"] = "answer"
    harness.KubeAgentsHarness._settle(result, [], ["t_1"], stalled=["t_1"])
    assert len(reads) == 1
    assert result.metadata["worker_commands"]
    assert (tmp_path / "worker-logs" / "t_1.log").read_text().startswith("  ┊ 💻 preparing")


def test_a_stalled_card_keeps_the_whole_transcript_not_only_its_commands(monkeypatch, tmp_path):
    # The point of the dump: _worker_commands throws away every line that is
    # not a shell command, and those are the lines that say where it stopped.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: _LOG)
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    harness._dump_worker_logs(harness._worker_logs(["t_stuck"], 5.0), ["t_stuck"])
    written = (tmp_path / "worker-logs" / "t_stuck.log").read_text()
    assert "🔎 preparing search_files…" in written
    assert "some unrelated line 12s" in written


def test_a_card_that_settled_leaves_no_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: _LOG)
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    harness._dump_worker_logs(harness._worker_logs(["t_done"], 5.0), [])
    assert not (tmp_path / "worker-logs").exists()


def test_a_local_run_without_artifacts_writes_nothing(monkeypatch, tmp_path):
    # ARTIFACTS is Prow's; unset, the dump is a no-op rather than a guess at
    # where the run directory is.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: _LOG)
    monkeypatch.delenv("ARTIFACTS", raising=False)
    harness._dump_worker_logs(harness._worker_logs(["t_stuck"], 5.0), ["t_stuck"])
    assert list(tmp_path.iterdir()) == []


def test_an_unwritable_artifacts_directory_does_not_fail_the_run(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: _LOG)
    monkeypatch.setenv("ARTIFACTS", str(blocked))
    harness._dump_worker_logs(harness._worker_logs(["t_stuck"], 5.0), ["t_stuck"])


def test_the_sentinel_carries_the_files_mtime_and_size(monkeypatch):
    # An mtime frozen near claim time says the worker died as it started; one
    # still advancing at the ceiling says it was alive. Read in the same exec
    # as the body, because _purge_card_state deletes the file straight after.
    monkeypatch.setattr(
        harness, "_agent_shell", lambda script, timeout: "__WORKER_LOG__ 1758579012 4096\nbody\n"
    )
    log = harness._worker_logs(["t_stat"], 5.0)["t_stat"]
    assert (log.mtime, log.size) == ("1758579012", "4096")
    assert log.body == "body\n"


def test_a_stat_that_failed_still_yields_the_transcript(monkeypatch):
    # stat is the diagnostic, the body is the evidence: losing the first must
    # not lose the second.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "__WORKER_LOG__ - -\nbody\n")
    log = harness._worker_logs(["t_nostat"], 5.0)["t_nostat"]
    assert (log.mtime, log.size) == ("-", "-")
    assert log.body == "body\n"


def test_the_index_records_a_stalled_card_that_has_no_transcript(monkeypatch, tmp_path):
    # The run this was written for: every stalled card absent, because the
    # dispatcher never spawned a worker. Returning early on an empty map would
    # leave that indistinguishable from a dump that did not run.
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "__NO_WORKER_LOG__\n")
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    harness._dump_worker_logs(harness._worker_logs(["t_never"], 5.0), ["t_never"])
    index = (tmp_path / "worker-logs" / "index.txt").read_text().splitlines()
    assert index[0].split("\t") == ["card", "mtime_epoch", "size_bytes", "state"]
    assert index[1] == "t_never\t-\t-\t__NO_WORKER_LOG__"
    assert not (tmp_path / "worker-logs" / "t_never.log").exists()


def test_the_index_records_the_stat_of_a_transcript_that_is_there(monkeypatch, tmp_path):
    monkeypatch.setattr(
        harness, "_agent_shell", lambda script, timeout: "__WORKER_LOG__ 1758579012 4096\nbody\n"
    )
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    harness._dump_worker_logs(harness._worker_logs(["t_stuck"], 5.0), ["t_stuck"])
    index = (tmp_path / "worker-logs" / "index.txt").read_text().splitlines()
    assert index[1] == "t_stuck\t1758579012\t4096\t__WORKER_LOG__"
    assert (tmp_path / "worker-logs" / "t_stuck.log").read_text() == "body\n"


def test_a_capture_that_failed_is_not_written_as_no_worker(monkeypatch, tmp_path):
    # _worker_logs maps a card to None when its exec failed. That says
    # nothing about the file, so the row must not read as "never had a
    # worker" -- the state that points a reader at the dispatcher.
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "")
    logs = harness._worker_logs(["t_unread"], 5.0)
    assert logs == {"t_unread": None}
    harness._dump_worker_logs(logs, ["t_unread"])
    index = (tmp_path / "worker-logs" / "index.txt").read_text().splitlines()
    assert index[1] == "t_unread\t-\t-\t__WORKER_LOG_UNREAD__"
    assert not (tmp_path / "worker-logs" / "t_unread.log").exists()


def test_a_transcript_that_would_not_write_is_not_indexed_as_present(monkeypatch, tmp_path):
    # Review finding: the index was appended before the .log files, so a body
    # write that failed left a "present" row pointing at a file that is not
    # there. Bodies go first now, and the one that failed keeps its stat under
    # its own state; the sibling's file and the index are still written.
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    monkeypatch.setattr(
        harness, "_agent_shell", lambda script, timeout: "__WORKER_LOG__ 1758579012 83\nbody\n"
    )
    (tmp_path / "worker-logs").mkdir()
    (tmp_path / "worker-logs" / "t_blocked.log").mkdir()  # write_text fails: is a directory
    harness._dump_worker_logs(harness._worker_logs(["t_blocked", "t_ok"], 5.0), ["t_blocked", "t_ok"])
    index = (tmp_path / "worker-logs" / "index.txt").read_text().splitlines()
    assert index[1:] == [
        "t_blocked\t1758579012\t83\t__WORKER_LOG_UNWRITTEN__",
        "t_ok\t1758579012\t83\t__WORKER_LOG__",
    ]
    assert (tmp_path / "worker-logs" / "t_ok.log").read_text() == "body\n"


def test_a_second_episode_appends_to_the_index_rather_than_replacing_it(monkeypatch, tmp_path):
    # Every repetition in a build dumps into the same ARTIFACTS directory. The
    # first version of this wrote the index whole, and a build with three
    # stalled repetitions kept one row.
    monkeypatch.setenv("ARTIFACTS", str(tmp_path))
    monkeypatch.setattr(harness, "_agent_shell", lambda script, timeout: "__NO_WORKER_LOG__\n")
    harness._dump_worker_logs(harness._worker_logs(["t_first"], 5.0), ["t_first"])
    monkeypatch.setattr(
        harness, "_agent_shell", lambda script, timeout: "__WORKER_LOG__ 1758579012 4096\nbody\n"
    )
    harness._dump_worker_logs(harness._worker_logs(["t_second"], 5.0), ["t_second"])
    index = (tmp_path / "worker-logs" / "index.txt").read_text().splitlines()
    assert index == [
        "card\tmtime_epoch\tsize_bytes\tstate",
        "t_first\t-\t-\t__NO_WORKER_LOG__",
        "t_second\t1758579012\t4096\t__WORKER_LOG__",
    ]


def test_stash_carries_none_when_nothing_was_captured():
    transcript.set("x", [])
    assert transcript.get().worker_commands is None
    transcript.set("x", [], worker_commands=[])
    assert transcript.get().worker_commands == []
