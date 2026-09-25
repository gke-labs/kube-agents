"""The harness reads the delegated workers' tool calls back into the run record.

Two layers are testable without a cluster. The in-pod script runs here under
the test interpreter against a temporary data root laid out like the agent's
``/opt/data`` -- a kanban board holding a front card, the Cluster Agent card it
fanned out and their runs, plus one session store per profile in hermes'
schema -- and the harness side is fed the script's own reply. What is asserted
is the contract the record depends on: the walk from the front card to every
worker, the card-to-session match, the pairing of each call with its result,
the tags, the scrubbing (by the repository's own redactor, the file the image
ships) and the clipping after it, and that ``_settle`` appends the entries
after the router's calls and before the purge.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from devops_bench.agents import AgentResult

from kube_agents_bench import harness, transcript, worker_trajectory
from kube_agents_bench.verifiers import ToolCalledVerifier

FRONT = "t_front01"
CHILD = "t_child02"
OTHER = "t_other03"
PLATFORM_SESSION = "20260917_182332_c7b328"
CLUSTER_SESSION = "20260917_183001_9a1b2c"
OTHER_SESSION = "20260917_170000_000000"
TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
REDACTED = "[REDACTED_SECRET]"
# The redactor the pod loads from the image, at its source path here.
REDACTOR = (
    Path(__file__).resolve().parents[2] / "agents" / "chat" / "defaults" / "plugins" / "common"
) / "redactor.py"

_BOARD_DDL = (
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,"
    " created_by TEXT, created_at INTEGER)",
    "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
    " profile TEXT, status TEXT, started_at INTEGER, ended_at INTEGER, summary TEXT,"
    " metadata TEXT)",
    "CREATE TABLE task_links (parent_id TEXT, child_id TEXT, PRIMARY KEY (parent_id, child_id))",
)
_CHILDREN_DDL = (
    "CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, creator_id TEXT,"
    " created_at INTEGER)"
)
# ``messages`` only: the read never opens hermes' ``sessions`` table.
_STORE_DDL = (
    "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
    " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL,"
    " active INTEGER DEFAULT 1)",
)


def _store(path: Path, session: str, messages: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        for ddl in _STORE_DDL:
            conn.execute(ddl)
        for index, (role, content, tool_call_id, tool_calls, tool_name) in enumerate(messages):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls,"
                " tool_name, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session,
                    role,
                    content,
                    tool_call_id,
                    json.dumps(tool_calls) if tool_calls is not None else None,
                    tool_name,
                    1000.0 + index,
                ),
            )


def _board(root: Path, *, children_table: bool = True) -> None:
    with sqlite3.connect(root / "kanban.db") as conn:
        for ddl in _BOARD_DDL:
            conn.execute(ddl)
        if children_table:
            conn.execute(_CHILDREN_DDL)
        conn.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, 'done', ?, 1)",
            [
                (FRONT, "Fix the checkout outage", "platform", "default"),
                (CHILD, "Read the shop namespace", "cluster-abc", "platform"),
                (OTHER, "Some earlier run", "platform", "default"),
            ],
        )
        conn.executemany(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, metadata)"
            " VALUES (?, ?, 'done', 1, 2, ?)",
            [
                (FRONT, "platform", "{}"),
                (CHILD, "cluster-abc", json.dumps({"worker_session_id": CLUSTER_SESSION})),
            ],
        )
        if children_table:
            conn.execute("INSERT INTO kanban_worker_children VALUES (?, ?, 1)", (CHILD, FRONT))
        else:
            conn.execute("INSERT INTO task_links VALUES (?, ?)", (FRONT, CHILD))


@pytest.fixture(autouse=True)
def no_cluster_exec(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Keep ``_settle`` off whatever cluster kubectl is pointed at.

    Same guard as the harness suite's: settling reads the pod and then
    deletes the card's state through ``kubectl exec``, and left live the two
    ``_settle`` tests below would aim those at the developer's current
    context. Returns the scripts that would have run.
    """
    scripts: list[str] = []

    def _record(script: str, timeout: float) -> str:
        scripts.append(script)
        return ""

    monkeypatch.setattr(harness, "_agent_shell", _record)
    return scripts


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """An agent data volume: the board plus one session store per profile."""
    _board(tmp_path)
    # The platform worker: a shell read whose result carries a token, then a
    # kanban_create that failed. The first call is stored in hermes' flat
    # ``{"name", "arguments"}`` shape and paired by order; the second in the
    # OpenAI shape with an id and paired by ``tool_call_id``.
    _store(
        tmp_path / "profiles" / "platform" / "state.db",
        PLATFORM_SESSION,
        [
            ("user", f"work kanban task {FRONT}", None, None, None),
            (
                "assistant",
                None,
                None,
                [{"name": "terminal", "arguments": json.dumps({"command": "kubectl get pods"})}],
                None,
            ),
            ("tool", f"NAME READY\ncheckout 0/1\nGH_TOKEN={TOKEN}", None, None, None),
            (
                "assistant",
                "Filing a card for the cluster agent.",
                None,
                [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "kanban_create",
                            "arguments": json.dumps({"title": "Read the shop namespace"}),
                        },
                    }
                ],
                None,
            ),
            ("tool", json.dumps({"ok": False, "error": "board is full"}), "call_2", None, None),
            ("assistant", "Done.", None, None, None),
        ],
    )
    # Another card's session in the same store: must not be read.
    with sqlite3.connect(tmp_path / "profiles" / "platform" / "state.db") as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, 1)",
            (OTHER_SESSION, f"work kanban task {OTHER}"),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES"
            " (?, 'assistant', ?, 2)",
            (OTHER_SESSION, json.dumps([{"name": "terminal", "arguments": "{}"}])),
        )
    # The Cluster Agent, in the per-cluster profile's own store.
    _store(
        tmp_path / "profiles" / "cluster-abc" / "state.db",
        CLUSTER_SESSION,
        [
            ("user", f"work kanban task {CHILD}", None, None, None),
            (
                "assistant",
                None,
                None,
                [{"name": "kubectl_get", "arguments": json.dumps({"namespace": "shop"})}],
                None,
            ),
            ("tool", "pods: checkout CrashLoopBackOff", None, None, "kubectl_get"),
        ],
    )
    return tmp_path


def _run_script(
    root: Path,
    task_ids: list[str],
    *,
    max_cards: int = 32,
    max_calls: int = 2000,
    max_result: int = 2000,
    redactor: Path = REDACTOR,
) -> str:
    """The in-pod script under the test interpreter, with the caps the harness passes."""
    if not REDACTOR.exists():
        pytest.skip(f"{REDACTOR} is not beside this checkout")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            worker_trajectory._IN_POD_SCRIPT,
            str(root),
            worker_trajectory.CAPTURE_PRESENT,
            str(redactor),
            str(max_cards),
            str(max_calls),
            str(max_result),
            "2000",
            *task_ids,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _payload(reply: str) -> dict:
    marker, _, body = reply.partition(worker_trajectory.CAPTURE_PRESENT)
    assert marker.strip() == ""
    return json.loads(body)


def _read_result(root: Path, content) -> dict:
    """Plant one terminal call whose result row is ``content`` and read it back.

    ``content`` is stored as hermes stores it: a string as is, anything else
    under the ``\\x00json:`` prefix. Returns the call's entry from the payload.
    """
    stored = content if isinstance(content, str) else "\x00json:" + json.dumps(content)
    with sqlite3.connect(root / "profiles" / "platform" / "state.db") as conn:
        conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND role != 'user'", (PLATFORM_SESSION,)
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp)"
            " VALUES (?, 'assistant', ?, 2)",
            (PLATFORM_SESSION, json.dumps([{"id": "c1", "name": "terminal", "arguments": "{}"}])),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, timestamp)"
            " VALUES (?, 'tool', ?, 'c1', 3)",
            (PLATFORM_SESSION, stored),
        )
    calls = _payload(_run_script(root, [FRONT]))["calls"]
    return next(c for c in calls if c["task"] == FRONT)


# ------------------------------------------------------------ the in-pod read


def test_the_read_walks_from_the_front_card_to_the_cluster_agent(data_root: Path) -> None:
    payload = _payload(_run_script(data_root, [FRONT]))

    cards = {c["task"]: c for c in payload["cards"]}
    assert list(cards) == [FRONT, CHILD]
    assert cards[FRONT]["children"] == [CHILD]
    # The platform card had no stamp, so its session came from the prompt;
    # the cluster card's run carried hermes' own stamp.
    assert [(s["id"], s["agent"], s["match"]) for s in cards[FRONT]["sessions"]] == [
        (PLATFORM_SESSION, "platform", "worker_prompt")
    ]
    assert [(s["id"], s["agent"], s["match"]) for s in cards[CHILD]["sessions"]] == [
        (CLUSTER_SESSION, "cluster-abc", "run_metadata")
    ]
    assert payload["errors"] == []
    assert payload["truncated"] is False


def test_each_call_is_paired_with_its_result_and_tagged(data_root: Path) -> None:
    calls = _payload(_run_script(data_root, [FRONT]))["calls"]

    assert [(c["name"], c["agent"], c["task"], c["session"]) for c in calls] == [
        ("terminal", "platform", FRONT, PLATFORM_SESSION),
        ("kanban_create", "platform", FRONT, PLATFORM_SESSION),
        ("kubectl_get", "cluster-abc", CHILD, CLUSTER_SESSION),
    ]
    # Paired by order (no id on either side) ...
    assert calls[0]["result"].startswith("NAME READY")
    # ... and by tool_call_id.
    assert json.loads(calls[1]["result"]) == {"ok": False, "error": "board is full"}
    assert calls[2]["result"] == "pods: checkout CrashLoopBackOff"
    # The failure verdict is taken in the pod, on the unclipped result.
    assert [c["failed"] for c in calls] == [False, True, False]
    assert all(isinstance(c["at"], float) for c in calls)
    assert "_id" not in calls[0]


def test_a_call_id_keyed_call_is_paired_by_it(data_root: Path) -> None:
    """The other id spelling some tool-call serialisations use."""
    with sqlite3.connect(data_root / "profiles" / "cluster-abc" / "state.db") as conn:
        conn.execute("DELETE FROM messages WHERE role != 'user'")
        conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp)"
            " VALUES (?, 'assistant', ?, 2)",
            (
                CLUSTER_SESSION,
                json.dumps([{"call_id": "c9", "name": "kubectl_get", "arguments": "{}"}]),
            ),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, timestamp)"
            " VALUES (?, 'tool', 'one pod', 'c9', 3)",
            (CLUSTER_SESSION,),
        )
    cluster = [c for c in _payload(_run_script(data_root, [FRONT]))["calls"] if c["task"] == CHILD]
    assert [c["result"] for c in cluster] == ["one pod"]


def test_a_result_whose_id_matches_no_call_is_an_orphan(data_root: Path) -> None:
    """A stray result row never lands on an unrelated pending call (parse_response's rule)."""
    with sqlite3.connect(data_root / "profiles" / "cluster-abc" / "state.db") as conn:
        conn.execute("UPDATE messages SET tool_call_id = 'c_unknown' WHERE role = 'tool'")
    payload = _payload(_run_script(data_root, [FRONT]))
    cluster = [c for c in payload["calls"] if c["task"] == CHILD]
    assert [c["result"] for c in cluster] == [None]
    assert any("1 tool result(s) matched no call" in e for e in payload["errors"])
    # The call itself was read and tagged, so the note is not a read gap.
    assert payload["unread"] == []


def test_another_cards_session_in_the_same_store_is_not_read(data_root: Path) -> None:
    calls = _payload(_run_script(data_root, [FRONT]))["calls"]
    assert OTHER_SESSION not in {c["session"] for c in calls}
    assert OTHER not in {c["task"] for c in calls}


def test_a_card_whose_id_extends_this_one_is_not_this_card(data_root: Path) -> None:
    """``LIKE '%... t_front01%'`` alone would also take ``t_front012``'s session."""
    longer = "20260917_200000_longer"
    with sqlite3.connect(data_root / "profiles" / "platform" / "state.db") as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, 9)",
            (longer, f"work kanban task {FRONT}2"),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES"
            " (?, 'assistant', ?, 10)",
            (longer, json.dumps([{"name": "kanban_complete", "arguments": "{}"}])),
        )
    cards = {c["task"]: c for c in _payload(_run_script(data_root, [FRONT]))["cards"]}
    assert [s["id"] for s in cards[FRONT]["sessions"]] == [PLATFORM_SESSION]


def test_a_continuation_card_is_walked_when_the_creator_table_is_absent(tmp_path: Path) -> None:
    """A base image without the fan-out patch still links cards by task_links."""
    _board(tmp_path, children_table=False)
    payload = _payload(_run_script(tmp_path, [FRONT]))
    assert [c["task"] for c in payload["cards"]] == [FRONT, CHILD]


def test_a_missing_session_store_is_reported_not_fatal(data_root: Path) -> None:
    """A profile pruned before the read costs its calls, and says so."""
    (data_root / "profiles" / "cluster-abc" / "state.db").unlink()

    payload = _payload(_run_script(data_root, [FRONT]))

    assert any("cluster-abc" in e for e in payload["errors"])
    assert any(f"no session found for card {CHILD}" == e for e in payload["errors"])
    assert {c["agent"] for c in payload["calls"]} == {"platform"}
    # A run was dispatched to cluster-abc, so its missing store hides work.
    assert payload["unread"] == [e for e in payload["errors"] if "cluster-abc" in e or CHILD in e]
    assert "no session store for profile cluster-abc" in payload["unread"]
    assert f"no session found for card {CHILD}" in payload["unread"]


def test_a_card_for_a_name_no_run_was_dispatched_to_is_not_a_read_gap(data_root: Path) -> None:
    """A composed assignee that is not a profile never runs: that is the observation."""
    with sqlite3.connect(data_root / "kanban.db") as conn:
        conn.execute("UPDATE tasks SET assignee = 'cluster-made-up' WHERE id = ?", (CHILD,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (CHILD,))

    payload = _payload(_run_script(data_root, [FRONT]))

    assert "no session store for profile cluster-made-up" in payload["errors"]
    assert payload["unread"] == []
    captured = worker_trajectory.capture(lambda s, t: _run_script(data_root, [FRONT]), [FRONT], 5.0)
    assert worker_trajectory.gaps(captured.summary) == []


def test_a_card_not_on_the_board_is_reported(data_root: Path) -> None:
    payload = _payload(_run_script(data_root, ["t_missing"]))
    assert payload["cards"] == []
    assert payload["errors"] == ["card t_missing is not on the board"]
    assert payload["unread"] == payload["errors"]


def test_a_missing_board_still_answers_with_the_sentinel(tmp_path: Path) -> None:
    payload = _payload(_run_script(tmp_path, [FRONT]))
    assert payload["cards"] == [] and payload["calls"] == []
    assert payload["errors"] and payload["errors"][0].startswith("kanban board:")
    assert payload["unread"] == payload["errors"]


def test_long_results_are_clipped_in_the_pod(data_root: Path) -> None:
    calls = _payload(_run_script(data_root, [FRONT], max_result=10))["calls"]
    assert calls[0]["result"].startswith("NAME READY")
    assert calls[0]["result"].endswith("chars]")
    assert "[clipped" in calls[0]["result"]


def test_the_call_cap_stops_the_read_and_says_so(data_root: Path) -> None:
    """The cap is what keeps the exec output bounded, so it has to bite exactly."""
    payload = _payload(_run_script(data_root, [FRONT], max_calls=2))
    assert [c["name"] for c in payload["calls"]] == ["terminal", "kanban_create"]
    assert payload["truncated"] is True
    assert payload["clipped"] == ["calls"]


def test_a_call_cap_clip_is_not_reported_as_the_card_cap(data_root: Path) -> None:
    captured = worker_trajectory.capture(lambda s, t: _run_script(data_root, [FRONT], max_calls=2), [FRONT], 5.0)
    assert worker_trajectory.gaps(captured.summary) == [worker_trajectory.CALL_CAP_GAP]


def test_the_card_cap_stops_the_walk_and_says_so(data_root: Path) -> None:
    payload = _payload(_run_script(data_root, [FRONT], max_cards=1))
    assert [c["task"] for c in payload["cards"]] == [FRONT]
    assert payload["truncated"] is True
    assert payload["clipped"] == ["cards"]
    assert CHILD not in {c["task"] for c in payload["calls"]}


def test_a_call_without_a_result_row_stays_called(data_root: Path) -> None:
    """A worker killed mid-call leaves the call with no result; that is the record."""
    with sqlite3.connect(data_root / "profiles" / "cluster-abc" / "state.db") as conn:
        conn.execute("DELETE FROM messages WHERE role = 'tool'")
    reply = _run_script(data_root, [FRONT])
    cluster = [c for c in _payload(reply)["calls"] if c["task"] == CHILD]
    assert [c["result"] for c in cluster] == [None]
    captured = worker_trajectory.capture(lambda s, t: reply, [FRONT], 5.0)
    assert captured is not None
    assert [e["status"] for e in captured.entries if e["task"] == CHILD] == ["called"]


def test_a_retried_card_yields_every_session_it_ran_in(data_root: Path) -> None:
    """A card re-run on unblock has two sessions; both are the card's history."""
    retry = "20260917_190000_retry0"
    with sqlite3.connect(data_root / "profiles" / "platform" / "state.db") as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, 4)",
            (retry, f"work kanban task {FRONT}"),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES"
            " (?, 'assistant', ?, 5)",
            (retry, json.dumps([{"name": "kanban_complete", "arguments": "{}"}])),
        )
    cards = {c["task"]: c for c in _payload(_run_script(data_root, [FRONT]))["cards"]}
    assert [s["id"] for s in cards[FRONT]["sessions"]] == [PLATFORM_SESSION, retry]
    assert [s["calls"] for s in cards[FRONT]["sessions"]] == [2, 1]


def test_the_default_profile_reads_the_root_store(tmp_path: Path) -> None:
    """A card assigned to the front door's own profile lives in ``<root>/state.db``."""
    _board(tmp_path)
    with sqlite3.connect(tmp_path / "kanban.db") as conn:
        conn.execute("UPDATE tasks SET assignee = 'default' WHERE id = ?", (FRONT,))
        conn.execute("UPDATE task_runs SET profile = 'default' WHERE task_id = ?", (FRONT,))
    _store(
        tmp_path / "state.db",
        PLATFORM_SESSION,
        [
            ("user", f"work kanban task {FRONT}", None, None, None),
            ("assistant", None, None, [{"name": "kanban_list", "arguments": "{}"}], None),
            ("tool", "{}", None, None, "kanban_list"),
        ],
    )
    payload = _payload(_run_script(tmp_path, [FRONT]))
    front = [c for c in payload["calls"] if c["task"] == FRONT]
    assert [(c["name"], c["agent"]) for c in front] == [("kanban_list", "default")]


# ------------------------------------------------------------- harness side


def test_capture_yields_canonical_entries_with_secrets_scrubbed(data_root: Path) -> None:
    reply = _run_script(data_root, [FRONT])
    seen: list[str] = []

    def shell(script: str, timeout: float) -> str:
        seen.append(script)
        return reply

    captured = worker_trajectory.capture(shell, [FRONT], 5.0)

    assert captured is not None
    assert len(seen) == 1 and FRONT in seen[0]
    assert seen[0].startswith(f"PY={worker_trajectory.HERMES_PYTHON}")
    assert worker_trajectory.DATA_ROOT in seen[0]
    names = [e["name"] for e in captured.entries]
    assert names == ["terminal", "kanban_create", "kubectl_get"]
    first, failed, cluster = captured.entries
    assert set(first) == {"name", "args", "result", "status", "agent", "task", "session", "at"}
    assert first["args"] == {"command": "kubectl get pods"}
    assert first["status"] == "completed"
    assert TOKEN not in first["result"]
    assert f"GH_TOKEN={REDACTED}" in first["result"]
    # A structured hermes failure is status="error", as parse_response marks it.
    assert failed["status"] == "error"
    assert cluster["agent"] == "cluster-abc"
    assert captured.summary["calls"] == 3
    assert captured.summary["errors"] == []
    assert [c["task"] for c in captured.summary["cards"]] == [FRONT, CHILD]


def test_capture_is_none_when_the_read_did_not_run() -> None:
    # kubectl failed, or the interpreter was missing: no sentinel at all.
    assert worker_trajectory.capture(lambda script, timeout: "", [FRONT], 5.0) is None
    assert (
        worker_trajectory.capture(lambda script, timeout: "sh: python3: not found\n", [FRONT], 5.0)
        is None
    )


def test_capture_is_none_when_no_card_was_delegated() -> None:
    calls: list[str] = []
    assert worker_trajectory.capture(lambda s, t: calls.append(s) or "", [], 5.0) is None
    assert calls == []


def test_capture_survives_a_sentinel_without_json() -> None:
    reply = f"{worker_trajectory.CAPTURE_PRESENT}\nTraceback (most recent call last)"
    assert worker_trajectory.capture(lambda s, t: reply, [FRONT], 5.0) is None


# ------------------------------------------------------ scrubbing, in the pod


@pytest.mark.parametrize(
    ("raw", "kept", "gone"),
    [
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "Authorization:", "abcdefgh"),
        ("Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==", "Authorization:", "QWxhZGRpbj"),
        (f"export GH_TOKEN={TOKEN}", "export GH_TOKEN=", TOKEN),
        ("ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrstuv", "ANTHROPIC_API_KEY=", "abcdefghij"),
        ("SLACK_BOT=xoxb-1234567890-abcdefghij", "SLACK_BOT=", "1234567890"),
        ("https://user:hunter22@github.com/org/repo.git", "@github.com/org/repo.git", "hunter22"),
        (
            "-----BEGIN PRIVATE KEY-----\nMIIE...\n-----END PRIVATE KEY-----",
            "[REDACTED_PRIVATE_KEY]",
            "MIIE",
        ),
        ('{"password": "s3cretvalue"}', '"password": "', "s3cretvalue"),
        ("ya29.a0AfH6SMBxxxxxxxxxxxxxxxxxxxxxxxx", REDACTED, "a0AfH6"),
    ],
)
def test_the_pod_redacts_credential_shapes_with_the_installs_redactor(
    data_root: Path, raw: str, kept: str, gone: str
) -> None:
    out = _read_result(data_root, raw)["result"]
    assert kept in out
    assert gone not in out


def test_a_secrets_yaml_block_is_blanked_including_block_scalars(data_root: Path) -> None:
    """``kubectl get secret -o yaml``: pairs, and the lines under a ``|`` scalar."""
    yaml = (
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: tls\n  namespace: shop\n"
        "data:\n  tls.key: TFMwdExTMUNSVWRKVGlCUVVrbFdRVlJGSUV0RldTMHRMUzB0\n"
        "stringData:\n  config.txt: |\n    first line with key: val\n"
        "    SUPER_SECRET_LINE_WITHOUT_COLON_12345\n    ANOTHER_LINE\n"
        "type: kubernetes.io/tls\n"
    )
    out = _read_result(data_root, yaml)["result"]
    for gone in ("TFMwdExT", "SUPER_SECRET", "ANOTHER_LINE", "first line"):
        assert gone not in out
    assert f"  tls.key: {REDACTED}\n" in out
    assert f"  config.txt: {REDACTED}\n    {REDACTED}\n    {REDACTED}\n    {REDACTED}\n" in out
    assert "namespace: shop" in out and "type: kubernetes.io/tls" in out


def test_a_secrets_json_block_is_blanked_before_it_is_clipped(data_root: Path) -> None:
    """``-o json`` with values longer than the clip: the cut lands after the scrub."""
    big = "Q" * 3000
    js = json.dumps(
        {
            "apiVersion": "v1",
            "data": {"credentials.json": big, "ca.crt": "Q0VSVA=="},
            "kind": "Secret",
        }
    )
    entry = _read_result(data_root, js)
    assert "QQQQ" not in entry["result"] and "Q0VSVA" not in entry["result"]
    assert f'"credentials.json": "{REDACTED}", "ca.crt": "{REDACTED}"' in entry["result"]
    # An unclosed block (a tool that cut its own output) is blanked to the end.
    cut = js[: js.index("Q0VSVA") + 3]
    assert not cut.endswith("}") and "Q0V" in cut
    assert "QQQQ" not in _read_result(data_root, cut)["result"]


def test_a_secrets_last_applied_annotation_is_blanked_too(data_root: Path) -> None:
    """``-o json`` text carries the whole Secret again, escaped, in an annotation."""
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "stringData": {"password": "hunter2-plaintext"},
        "metadata": {"name": "db"},
    }
    applied = {
        "apiVersion": "v1",
        "data": {"password": "aHVudGVyMg=="},
        "kind": "Secret",
        "metadata": {
            "name": "db",
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": json.dumps(secret)},
        },
    }
    out = _read_result(data_root, json.dumps(applied, indent=2))["result"]
    assert "hunter2" not in out and "aHVudGVy" not in out
    inner = json.loads(
        json.loads(out)["metadata"]["annotations"][
            "kubectl.kubernetes.io/last-applied-configuration"
        ]
    )
    assert inner["stringData"] == {"password": REDACTED} and inner["metadata"] == {"name": "db"}


def test_named_values_in_free_text_are_blanked_by_the_redactors_key_words(data_root: Path) -> None:
    """``kubectl describe pod`` env lines and a kubeconfig dump: names the redactor's
    mapping walk would blank, in text its patterns do not reach."""
    text = (
        "    Environment:\n      AWS_SECRET_ACCESS_KEY:  AKIAIOSFODNN7EXAMPLE99\n"
        "      LOG_LEVEL:  debug\n      DB_PASSWORD=pl4in\n"
        "users:\n- user:\n    client-key-data: TFMwdExTMUNSVWRKVGlC\n"
        "    client-certificate-data: Q0VSVA==\n"
    )
    out = _read_result(data_root, text)["result"]
    for gone in ("AKIAIOSFODNN7EXAMPLE99", "pl4in", "TFMwdExT", "Q0VSVA"):
        assert gone not in out
    assert f"AWS_SECRET_ACCESS_KEY:  {REDACTED}" in out
    assert f"DB_PASSWORD={REDACTED}" in out
    assert f"client-key-data: {REDACTED}" in out
    assert "LOG_LEVEL:  debug" in out


def test_a_structured_result_is_scrubbed_where_its_strings_sit(data_root: Path) -> None:
    """A ``\\x00json:`` dict: a ``data`` mapping is blanked, and YAML inside a
    string field is seen with its newlines rather than as escaped text."""
    result = {
        "kind": "Secret",
        "data": {"token": "dG9rZW4=", "count": 2},
        "output": "apiVersion: v1\ndata:\n  k: dmFsdWU=\nkind: Secret",
        "note": f"see https://bob:pw12345@example.com/x and {TOKEN}",
        "password": "hunter2",
        "spec": {"clientSecret": "plain-words", "image": "nginx"},
    }
    out = json.loads(_read_result(data_root, result)["result"])
    assert out["data"] == {"token": REDACTED, "count": 2}
    assert f"  k: {REDACTED}" in out["output"] and "dmFsdWU" not in out["output"]
    assert "pw12345" not in out["note"] and TOKEN not in out["note"]
    # The redactor's own walk: a sensitive-named key is blanked whatever its value.
    assert out["password"] == REDACTED
    assert out["spec"] == {"clientSecret": REDACTED, "image": "nginx"}


def test_a_configmaps_data_is_blanked_as_the_audit_log_blanks_it(data_root: Path) -> None:
    """By shape, not by kind: the redactor's documented choice, kept here."""
    cm = "apiVersion: v1\ndata:\n  LOG_LEVEL: debug\nkind: ConfigMap\n"
    out = _read_result(data_root, cm)["result"]
    assert f"  LOG_LEVEL: {REDACTED}\n" in out and "kind: ConfigMap" in out


def test_a_clipped_structured_failure_still_reads_as_an_error(data_root: Path) -> None:
    """The verdict is taken on the unclipped result, so the clip cannot hide it."""
    entry = _read_result(data_root, {"exit_code": 1, "output": "x" * 3000})
    assert entry["failed"] is True and entry["result"].endswith("chars]")
    status = [
        e["status"]
        for e in worker_trajectory.capture(
            lambda s, t: _run_script(data_root, [FRONT]), [FRONT], 5.0
        ).entries
        if e["task"] == FRONT
    ]
    assert status == ["error"]


def test_arguments_are_scrubbed_too(data_root: Path) -> None:
    with sqlite3.connect(data_root / "profiles" / "platform" / "state.db") as conn:
        conn.execute(
            "UPDATE messages SET tool_calls = ?"
            " WHERE role = 'assistant' AND tool_calls LIKE '%get pods%'",
            (
                json.dumps(
                    [
                        {
                            "name": "terminal",
                            "arguments": json.dumps(
                                {"command": f"gh auth login --with-token <<< {TOKEN}"}
                            ),
                        }
                    ]
                ),
            ),
        )
    captured = worker_trajectory.capture(lambda s, t: _run_script(data_root, [FRONT]), [FRONT], 5.0)
    assert captured is not None
    first = captured.entries[0]
    assert TOKEN not in json.dumps(first["args"])
    assert first["args"]["command"].endswith(REDACTED)


def test_a_manifest_inside_an_argument_is_scrubbed_with_its_newlines(data_root: Path) -> None:
    """The arguments are a JSON string; the YAML inside is seen as YAML, not as ``\\n`` text."""
    manifest = (
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: tls\ndata:\n  tls.key: TFMwdExTMUNS\n"
    )
    with sqlite3.connect(data_root / "profiles" / "platform" / "state.db") as conn:
        conn.execute(
            "UPDATE messages SET tool_calls = ?"
            " WHERE role = 'assistant' AND tool_calls LIKE '%get pods%'",
            (
                json.dumps(
                    [
                        {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "secret.yaml", "content": manifest}),
                        }
                    ]
                ),
            ),
        )
    captured = worker_trajectory.capture(lambda s, t: _run_script(data_root, [FRONT]), [FRONT], 5.0)
    first = captured.entries[0]
    assert "TFMwdExT" not in json.dumps(first["args"])
    assert first["args"]["path"] == "secret.yaml"
    assert f"  tls.key: {REDACTED}" in first["args"]["content"]


def test_ordinary_output_is_left_alone(data_root: Path) -> None:
    text = "NAME READY STATUS\ncheckout-7d9f 0/1 CrashLoopBackOff\nservice token-refresher ok"
    assert _read_result(data_root, text)["result"] == text


def test_a_pod_without_the_redactor_withholds_content_and_says_so(data_root: Path) -> None:
    """Fail closed: names, tags and statuses come back, results and arguments do not."""
    reply = _run_script(data_root, [FRONT], redactor=data_root / "no-such-redactor.py")
    payload = _payload(reply)
    assert any(e.startswith("redactor ") and "withheld" in e for e in payload["errors"])
    assert payload["unread"] == []
    assert TOKEN not in reply
    captured = worker_trajectory.capture(lambda s, t: reply, [FRONT], 5.0)
    assert [e["name"] for e in captured.entries] == ["terminal", "kanban_create", "kubectl_get"]
    assert [e["status"] for e in captured.entries] == ["completed", "error", "completed"]
    assert all("WITHHELD" in e["result"] for e in captured.entries)
    assert all(e["args"] == {"raw": e["result"]} for e in captured.entries)


def test_settle_appends_worker_calls_after_the_routers_and_before_the_purge(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = _run_script(data_root, [FRONT])
    scripts: list[str] = []

    def shell(script: str, timeout: float) -> str:
        scripts.append(script)
        return reply if worker_trajectory.CAPTURE_PRESENT in script else ""

    monkeypatch.setattr(harness, "_agent_shell", shell)
    router_call = {
        "name": "kanban_create",
        "args": {"title": "Fix the checkout outage"},
        "result": json.dumps({"ok": True, "task_id": FRONT}),
        "status": "completed",
    }
    result = AgentResult(output="filed", trajectory=[router_call])
    result.metadata["final_message"] = "filed"

    harness.KubeAgentsHarness._settle(result, [router_call], [FRONT])

    assert result.trajectory[0] is router_call
    assert [e["name"] for e in result.trajectory[1:]] == [
        "terminal",
        "kanban_create",
        "kubectl_get",
    ]
    assert {e["agent"] for e in result.trajectory[1:]} == {"platform", "cluster-abc"}
    assert result.metadata["worker_trajectory"]["calls"] == 3
    assert "rm -rf" in scripts[-1]
    assert worker_trajectory.CAPTURE_PRESENT in scripts[-2]


def test_settle_records_none_when_the_read_did_not_run(no_cluster_exec: list[str]) -> None:
    result = AgentResult(output="filed", trajectory=[])
    result.metadata["final_message"] = "filed"
    harness.KubeAgentsHarness._settle(result, [], [FRONT])
    assert result.metadata["worker_trajectory"] is None
    assert result.trajectory == []
    assert any(worker_trajectory.CAPTURE_PRESENT in s for s in no_cluster_exec)


def test_gaps_is_none_when_the_read_did_not_run() -> None:
    assert worker_trajectory.gaps(None) is None


def test_gaps_lists_unread_reads_and_the_card_cap() -> None:
    gap = "no session store for profile cluster-x"
    summary = {"cards": [], "errors": [gap], "unread": [gap], "truncated": True, "clipped": ["cards"], "calls": 0}
    assert worker_trajectory.gaps(summary) == [gap, worker_trajectory.TRUNCATED_GAP]


def test_gaps_leaves_out_notes_that_hide_no_worker() -> None:
    notes = ["redactor x: missing; results and arguments withheld", "no session store for profile cluster-y"]
    summary = {"cards": [], "errors": notes, "unread": [], "truncated": False, "calls": 2}
    assert worker_trajectory.gaps(summary) == []


def test_gaps_is_empty_for_a_complete_read() -> None:
    assert worker_trajectory.gaps({"cards": [], "errors": [], "unread": [], "truncated": False, "calls": 3}) == []


# ---------------------------------------------------------- tool_called stays


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def test_tool_called_keeps_counting_only_the_routers_calls() -> None:
    """A worker's kanban_create must not trip the front door's safeguard.

    ``chat-routing-board-read`` wraps ``tool_called: [kanban_create]`` in a
    ``none`` compound to assert the router filed no card. The platform worker
    legitimately files cards of its own; those now sit in the same trajectory,
    tagged, and the count has to keep meaning what it meant.
    """
    transcript.set(
        "answer",
        [
            {"name": "kanban_list", "args": {}, "result": "{}", "status": "completed"},
            {
                "name": "kanban_create",
                "args": {},
                "result": "{}",
                "status": "completed",
                "agent": "platform",
                "task": FRONT,
                "session": PLATFORM_SESSION,
                "at": 1.0,
            },
        ],
    )
    router_only = ToolCalledVerifier(type="tool_called", tool_names=["kanban_create"])
    assert router_only.verify(5.0).status == "fail"
    assert router_only.verify(5.0).raw == {"matching_calls": 0}
    board_read = ToolCalledVerifier(type="tool_called", tool_names=["kanban_list"])
    assert board_read.verify(5.0).status == "pass"
