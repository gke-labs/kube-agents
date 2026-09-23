"""The board read behind the delegation wait: what it returns, and when it refuses to.

The in-pod script runs here under the test interpreter against a board in a
temporary directory, the way ``test_worker_trajectory.py`` runs its sibling.
The harness-side parser is tested with canned replies.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from kube_agents_bench import board

FRONT = "t_9f2ac41b"
CHILD = "t_0c1d2e3f"
UNKNOWN = "t_ffffffff"


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    with sqlite3.connect(tmp_path / board.BOARD_FILE) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, assignee TEXT)")
        conn.executemany(
            "INSERT INTO tasks (id, status, assignee) VALUES (?, ?, ?)",
            [(FRONT, "running", "platform"), (CHILD, "done", "cluster-abc")],
        )
    return tmp_path


def _run_script(root: Path, task_ids: list[str]) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            board._IN_POD_SCRIPT,
            str(root),
            board.BOARD_FILE,
            board.BOARD_PRESENT,
            *task_ids,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _shell_for(root: Path):
    """A stand-in for ``harness._agent_shell`` that runs the script locally."""

    def shell(script: str, timeout: float) -> str:
        assert board.BOARD_PRESENT in script
        # The command line quotes the ids after the sentinel; recover them.
        ids = script.split(board.BOARD_PRESENT, 1)[1].split()
        return _run_script(root, [i.strip("'") for i in ids])

    return shell


def test_the_script_reports_each_known_card_and_omits_the_rest(data_root: Path) -> None:
    reply = _run_script(data_root, [FRONT, CHILD, UNKNOWN])
    marker, _, body = reply.partition(board.BOARD_PRESENT)
    assert marker.strip() == ""
    payload = json.loads(body)
    assert payload["error"] is None
    assert payload["statuses"] == {FRONT: "running", CHILD: "done"}


def test_a_missing_board_is_an_error_behind_the_sentinel(tmp_path: Path) -> None:
    """The sentinel says the script ran; the error says the read is not usable."""
    reply = _run_script(tmp_path, [FRONT])
    payload = json.loads(reply.partition(board.BOARD_PRESENT)[2])
    assert payload["statuses"] == {}
    assert "kanban board" in payload["error"]


def test_read_statuses_returns_the_boards_view_of_the_asked_cards(data_root: Path) -> None:
    statuses = board.read_statuses(_shell_for(data_root), [FRONT, CHILD, UNKNOWN], 5.0)
    assert statuses == {FRONT: "running", CHILD: "done"}


def test_read_statuses_is_none_when_the_board_cannot_be_opened(tmp_path: Path) -> None:
    """No board, no reading: the harness must fall back to a status turn."""
    assert board.read_statuses(_shell_for(tmp_path), [FRONT], 5.0) is None


def test_read_statuses_is_none_without_the_sentinel() -> None:
    """A kubectl that failed returns "" -- indistinguishable from an empty board
    without the sentinel, so it must not read as "no card settled"."""
    assert board.read_statuses(lambda script, timeout: "", [FRONT], 5.0) is None
    assert board.read_statuses(lambda script, timeout: "error: unable to exec", [FRONT], 5.0) is None


def test_read_statuses_is_none_on_a_sentinel_without_json() -> None:
    assert board.read_statuses(lambda s, t: f"{board.BOARD_PRESENT}\nnot json", [FRONT], 5.0) is None


def test_read_statuses_is_none_when_no_card_is_asked_for() -> None:
    calls: list[str] = []

    def shell(script: str, timeout: float) -> str:
        calls.append(script)
        return ""

    assert board.read_statuses(shell, [], 5.0) is None
    assert calls == []


def test_the_command_prefers_hermes_interpreter_and_names_the_cards() -> None:
    cmd = board.command([FRONT, CHILD])
    assert cmd.startswith(f"PY={board.HERMES_PYTHON}")
    assert board.FALLBACK_PYTHON in cmd
    assert board.DATA_ROOT in cmd
    assert cmd.index(FRONT) < cmd.index(CHILD)
