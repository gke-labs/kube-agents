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


# --- the cards one Hermes session filed (#2619) --------------------------------
#
# The bridge's api executor runs an inject conversation's turns in the session
# ``a2a-<contextId>``. These tables are hermes' shapes, trimmed to the columns
# the read opens.

SESSION = "a2a-ctx-67aed1ee09f614850d05a55e"
OTHER_SESSION = "a2a-ctx-0000000000000000000000ff"

STORE_DDL = (
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, end_reason TEXT,"
    " source TEXT, model_config TEXT, started_at REAL, ended_at REAL)",
    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
    " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)",
)
BOARD_DDL = (
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,"
    " result TEXT, created_at INTEGER)",
    "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT,"
    " status TEXT, summary TEXT, metadata TEXT)",
    "CREATE TABLE kanban_notify_subs (task_id TEXT NOT NULL, platform TEXT NOT NULL,"
    " chat_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,"
    " PRIMARY KEY (task_id, platform, chat_id, thread_id))",
    "CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, creator_id TEXT,"
    " created_at INTEGER)",
)


def build_session_store(
    root: Path,
    *,
    session: str = SESSION,
    created: list[str] = (),
    store_creates: bool = True,
    subscribe: bool = True,
) -> None:
    """A data root whose ``session`` filed ``created`` through ``kanban_create``.

    Each create is an assistant tool call and its tool result, as hermes stores
    a turn. ``store_creates=False`` leaves the session's messages out (a store
    that kept no tool rows), so only the subscriptions name the cards.
    """
    with sqlite3.connect(root / board.STORE_FILE) as conn:
        for ddl in STORE_DDL:
            conn.execute(ddl)
        conn.execute("INSERT INTO sessions (id) VALUES (?)", (session,))
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'user', 'check the fleet')",
            (session,),
        )
        for n, tid in enumerate(created if store_creates else []):
            call = f"call_{n}"
            conn.execute(
                "INSERT INTO messages (session_id, role, tool_calls) VALUES (?, 'assistant', ?)",
                (
                    session,
                    json.dumps(
                        [
                            {
                                "id": call,
                                "type": "function",
                                "function": {
                                    "name": "kanban_create",
                                    "arguments": json.dumps({"title": f"card {n}"}),
                                },
                            }
                        ]
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id) VALUES (?, 'tool', ?, ?)",
                (session, json.dumps({"ok": True, "task_id": tid}), call),
            )
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', 'On it.')",
            (session,),
        )
    with sqlite3.connect(root / board.BOARD_FILE) as conn:
        for ddl in BOARD_DDL:
            conn.execute(ddl)
        for tid in created:
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status) VALUES (?, 'x', 'platform', 'ready')",
                (tid,),
            )
            if subscribe:
                conn.execute(
                    "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at)"
                    " VALUES (?, 'api_server', ?, 0)",
                    (tid, session),
                )


def set_card(
    root: Path, tid: str, status: str, *, result: str | None = None, summary: str | None = None
) -> None:
    """Move ``tid`` on the board, as a worker claiming and completing it would."""
    with sqlite3.connect(root / board.BOARD_FILE) as conn:
        conn.execute("UPDATE tasks SET status = ?, result = ? WHERE id = ?", (status, result, tid))
        if summary is not None:
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, summary) VALUES (?, 'platform', ?, ?)",
                (tid, status, summary),
            )


def local_shell(root: Path, monkeypatch: pytest.MonkeyPatch, on_read=None):
    """A stand-in for ``harness._agent_shell`` running the session-cards read here.

    The command line itself runs under ``sh``, against ``root``, so the quoting
    is exercised too. Any other script gets an empty reply. ``on_read(n)`` runs
    before the n-th read (from 1), which is how a test moves the board between
    polls.
    """
    monkeypatch.setattr(board, "DATA_ROOT", str(root))
    monkeypatch.setattr(board, "HERMES_PYTHON", sys.executable)
    reads = []

    def shell(script: str, timeout: float) -> str:
        if board.SESSION_CARDS_PRESENT not in script:
            return ""
        reads.append(script)
        if on_read is not None:
            on_read(len(reads))
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    shell.reads = reads  # type: ignore[attr-defined]
    return shell


def test_the_api_session_id_matches_the_bridge() -> None:
    """Mirrors apiSessionID's verbatim half; an id the bridge would hash (unsafe,
    or over the cap) names no session here rather than a guessed one."""
    assert board.api_session_id("ctx-67aed1ee09f614850d05a55e") == SESSION
    assert board.api_session_id("x" * board.API_CONTEXT_ID_MAX_LEN) == "a2a-" + "x" * 128
    assert board.api_session_id("a/b") == ""
    assert board.api_session_id("x" * (board.API_CONTEXT_ID_MAX_LEN + 1)) == ""


def test_a_sessions_creates_name_its_cards_with_their_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_session_store(tmp_path, created=[FRONT, CHILD])
    set_card(tmp_path, FRONT, "done", result="three clusters, all healthy")
    set_card(tmp_path, CHILD, "done", summary="node pool resized")
    read = board.read_session_cards(local_shell(tmp_path, monkeypatch), SESSION, 5)
    assert read is not None and read.session_found
    assert read.card_ids == [FRONT, CHILD]
    assert read.source == "state.db"
    assert read.cards[FRONT] == {
        "status": "done",
        "result": "three clusters, all healthy",
        "summary": None,
    }
    assert read.cards[CHILD] == {"status": "done", "result": None, "summary": "node pool resized"}
    shown = json.loads(read.as_shown(CHILD)["result"])
    assert shown == {
        "task": {"id": CHILD, "status": "done", "result": None},
        "runs": [{"summary": "node pool resized"}],
    }


def test_subscriptions_name_the_cards_when_the_store_kept_no_creates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback, without a worker's child: children inherit their creator's
    subscriptions, and no wait follows a worker's children."""
    build_session_store(tmp_path, created=[FRONT], store_creates=False)
    with sqlite3.connect(tmp_path / board.BOARD_FILE) as conn:
        conn.execute("INSERT INTO tasks (id, status) VALUES (?, 'running')", (CHILD,))
        conn.execute(
            "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at) VALUES (?, 'api_server', ?, 0)",
            (CHILD, SESSION),
        )
        conn.execute(
            "INSERT INTO kanban_worker_children (child_id, creator_id) VALUES (?, ?)",
            (CHILD, FRONT),
        )
    read = board.read_session_cards(local_shell(tmp_path, monkeypatch), SESSION, 5)
    assert read is not None and read.session_found
    assert (read.card_ids, read.source) == ([FRONT], "kanban_notify_subs")


def test_another_sessions_cards_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_session_store(tmp_path, session=OTHER_SESSION, created=[FRONT])
    read = board.read_session_cards(local_shell(tmp_path, monkeypatch), SESSION, 5)
    assert read is not None
    assert (read.session_found, read.card_ids) == (False, [])


def test_a_compressed_session_is_followed_to_its_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_session = SESSION + "-2"
    build_session_store(tmp_path, session=child_session, created=[FRONT])
    with sqlite3.connect(tmp_path / board.STORE_FILE) as conn:
        conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE id = ?", (SESSION, child_session)
        )
        conn.execute("INSERT INTO sessions (id, end_reason) VALUES (?, 'compression')", (SESSION,))
    read = board.read_session_cards(local_shell(tmp_path, monkeypatch), SESSION, 5)
    assert read is not None and read.card_ids == [FRONT]


@pytest.mark.parametrize(
    "end_reason, source, model_config",
    [
        pytest.param("user_exit", None, None, id="a parent that did not end in compression"),
        pytest.param("compression", None, '{"_branched_from": "x"}', id="a /branch child"),
        pytest.param("compression", None, '{"_delegate_from": "x"}', id="a delegate child"),
        pytest.param("compression", "tool", None, id="a tool child"),
    ],
)
def test_only_a_compression_continuation_joins_the_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    end_reason: str,
    source: str | None,
    model_config: str | None,
) -> None:
    """Hermes' _CHAIN_STEP_SQL rule: any other child of the session is someone
    else's work, and its cards are not this run's."""
    child_session = SESSION + "-2"
    build_session_store(tmp_path, session=child_session, created=[FRONT])
    with sqlite3.connect(tmp_path / board.STORE_FILE) as conn:
        conn.execute(
            "UPDATE sessions SET parent_session_id = ?, source = ?, model_config = ? WHERE id = ?",
            (SESSION, source, model_config, child_session),
        )
        conn.execute("INSERT INTO sessions (id, end_reason) VALUES (?, ?)", (SESSION, end_reason))
    read = board.read_session_cards(local_shell(tmp_path, monkeypatch), SESSION, 5)
    assert read is not None and read.session_found
    assert read.card_ids == []


def test_an_unreadable_store_is_no_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No store at all is a failed read (``None``), never "no cards"."""
    assert board.read_session_cards(local_shell(tmp_path, monkeypatch), SESSION, 5) is None
    assert board.read_session_cards(lambda script, timeout: "", SESSION, 5) is None
