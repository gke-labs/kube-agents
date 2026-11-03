"""The card-wake replays: their prompts, their in-pod scripts, and how the turns become one result.

The in-pod scripts run here under the test interpreter, the way
``test_board.py`` runs its sibling, against a stand-in hermes tree: a
``kanban_db`` that keeps its board in a JSON file, and a notifier whose wake
passes through ``slack_ux_moments.wake_text`` when that module is there.
Where the tree has ``deploy/docker/patches/slack_ux_moments.py``, the tests
that need it copy the real one in, with the real ``agents/platform/scripts``
beside it; where it does not, they skip. The harness-side parsers are tested
with canned replies.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from devops_bench.agents import AgentResult
from kube_agents_bench import card_wake

REPO = Path(__file__).resolve().parents[2]
MOMENTS = REPO / "deploy" / "docker" / "patches" / "slack_ux_moments.py"
CLICKS = REPO / "deploy" / "docker" / "patches" / "slack_ux_clicks.py"
SCRIPTS = REPO / "agents" / "platform" / "scripts"

PROMPT = """[bench:slack-question-wake]
title: Check checkout-gateway's restarts
question: Two clusters run checkout-gateway. Which should I look at?
options: seeded-a | seeded-b
answer: seeded-b
"""
REASON = "Two clusters run checkout-gateway. Which should I look at?\n- seeded-a\n- seeded-b"
needs_moments = pytest.mark.skipif(
    not MOMENTS.exists(), reason="main has no deploy/docker/patches/slack_ux_moments.py"
)

_FAKE_KANBAN_DB = '''
import json, os, time
from dataclasses import dataclass
from types import SimpleNamespace

DEFAULT_BOARD = "default"
_PATH = os.environ["FAKE_BOARD"]


def _load():
    try:
        with open(_PATH) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"tasks": {}, "events": []}


def _save(state):
    with open(_PATH, "w") as fh:
        json.dump(state, fh)


class _Conn:
    # The archive script's query, a key's cards newest first (live only when
    # the query says so), the plant's sweep: other runs' live replay cards
    # filed before a cutoff, or the plant's backdating of a card, recorded as
    # it was asked.
    def execute(self, sql, params):
        if sql.startswith("UPDATE"):
            state = _load()
            state["tasks"][params[1]]["backdated"] = params[0]
            _save(state)
            return []
        assert "idempotency_key" in sql, sql
        tasks = _load()["tasks"]
        if "LIKE" in sql:
            prefix, key, cutoff = params
            return [(tid,) for tid, row in sorted(tasks.items())
                    if (row["key"] or "").startswith(prefix.rstrip("%")) and row["key"] != key
                    and row["status"] != "archived" and row["created_at"] < cutoff]
        live = "!= 'archived'" in sql
        return [(tid,) for tid in sorted(tasks, reverse=True)
                if tasks[tid]["key"] == params[0] and not (live and tasks[tid]["status"] == "archived")]


def connect():
    return _Conn()


def create_task(conn, *, title, body=None, created_by=None, priority=0, idempotency_key=None):
    state = _load()
    task_id = "t_%08d" % (len(state["tasks"]) + 1)
    state["tasks"][task_id] = {"title": title, "body": body, "created_by": created_by,
                               "status": "ready", "assignee": None, "key": idempotency_key,
                               "priority": priority, "comments": [],
                               "created_at": int(time.time())}
    _save(state)
    return task_id


def comment(task_id, author, body):
    state = _load()
    state["tasks"][task_id]["comments"].append({"author": author, "body": body})
    _save(state)


def list_comments(conn, task_id):
    return [SimpleNamespace(**c) for c in _load()["tasks"][task_id]["comments"]]


def block_task(conn, task_id, *, reason=None, kind=None):
    state = _load()
    state["tasks"][task_id]["status"] = "blocked"
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id, "kind": "blocked",
                            "payload": {"reason": reason, "kind": kind}})
    _save(state)
    return True


def unblock_task(conn, task_id):
    # As the real one: back to ready; the assignee is untouched.
    state = _load()
    task = state["tasks"][task_id]
    if task["status"] != "blocked":
        return False
    task["status"] = "ready"
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id,
                            "kind": "unblocked", "payload": None})
    _save(state)
    return True


def list_events(conn, task_id):
    return [SimpleNamespace(**e) for e in _load()["events"] if e["task_id"] == task_id]


@dataclass
class Task:
    id: str
    title: str
    status: str
    assignee: str | None


def get_task(conn, task_id):
    row = _load()["tasks"][task_id]
    return Task(id=task_id, title=row["title"], status=row["status"], assignee=row["assignee"])


def archive_task(conn, task_id):
    state = _load()
    state["tasks"][task_id]["status"] = "archived"
    _save(state)
    return True
'''

# A moments module that imports but never posts: a broken patch.
_SILENT_MOMENTS = '''
async def needs_you(adapter, sub, payload, event_id):
    return False
'''

_FAKE_NOTIFIER = '''
import os

try:
    from gateway.slack_ux_moments import wake_text as _kage_moments_wake_text
except ImportError:
    _kage_moments_wake_text = None


# The pinned locale's wake status for a blocked card.
_STATUS = {"blocked": "blocked; needs attention"}


class _KanbanNotification:
    def __init__(self, runner, d, *, platform_cls, sub_fail_counts):
        self.d, self.sub = d, d["sub"]
        self.adapter, self.synth, self.wake_kinds = None, "", set()

    def format_event(self, ev):
        return None

    def build_wake_text(self):
        if os.environ.get("FAKE_WAKE_FAILS"):
            raise RuntimeError("notifier exploded")
        self.wake_kinds = {ev.kind for ev in self.d["events"]}
        status = ", ".join(_STATUS[k] for k in _STATUS if k in self.wake_kinds)
        self.synth = "[kanban] Task %s %s." % (self.sub["task_id"], status)
        if self.d["task"].assignee:
            self.synth += "\\nAssignee: @%s" % self.d["task"].assignee
        if _kage_moments_wake_text is not None:
            self.synth = _kage_moments_wake_text(self.sub, self.d["events"], self.wake_kinds, self.synth)
'''


@pytest.fixture
def hermes_root(tmp_path: Path) -> Path:
    """A stand-in for ``/opt/hermes`` without ``gateway/slack_ux_moments.py``, as on main."""
    root = tmp_path / "hermes"
    (root / "hermes_cli").mkdir(parents=True)
    (root / "gateway").mkdir()
    (root / "hermes_cli" / "__init__.py").write_text("")
    (root / "hermes_cli" / "kanban_db.py").write_text(_FAKE_KANBAN_DB)
    # The split module opens the board, as on the image.
    (root / "hermes_cli" / "kanban_db_connect.py").write_text("from hermes_cli.kanban_db import connect\n")
    (root / "gateway" / "__init__.py").write_text("")
    (root / "gateway" / "kanban_watchers_notifier.py").write_text(_FAKE_NOTIFIER)
    return root


def _with_moments(root: Path) -> Path:
    shutil.copy(MOMENTS, root / "gateway" / "slack_ux_moments.py")
    return root


def _board(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "board.json").read_text())


def _run(script: str, args: list[str], tmp_path: Path, **env: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        check=False,
        env={"FAKE_BOARD": str(tmp_path / "board.json"), "PATH": "/usr/bin:/bin", **env},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


KEY = f"{card_wake.REPLAY_KEY_PREFIX}test"


def _shell_for(root: Path, tmp_path: Path, prompt: str = PROMPT, **env: str):
    """A stand-in for ``harness._agent_shell`` that runs the scripts locally.

    Each command must be the one :func:`card_wake.plant_command`,
    :func:`card_wake.read_command` or :func:`card_wake.archive_command` builds
    for :data:`KEY`; it runs with
    the stand-in tree in place of ``/opt/hermes`` and ``/opt/defaults/scripts``.
    """
    replay = card_wake.parse(prompt)
    assert replay is not None
    commands = {
        card_wake.plant_command(replay, KEY): card_wake._PLANT_SCRIPT,
        card_wake.read_command(KEY): card_wake._READ_SCRIPT,
        card_wake.archive_command(KEY): card_wake._ARCHIVE_SCRIPT,
    }

    def shell(command: str, timeout: float) -> str:
        script = commands[command]
        tokens = shlex.split(command)
        args = tokens[tokens.index("-c") + 2 :]
        args[1] = str(root)
        if script is card_wake._PLANT_SCRIPT:
            args[2] = str(SCRIPTS)
        return _run(script, args, tmp_path, **env)

    return shell


def _plant(shell, prompt: str = PROMPT) -> card_wake.Planted:
    return card_wake.plant(shell, card_wake.parse(prompt), timeout=30, key=KEY)


def _moments_note() -> str:
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("slack_ux_moments_note", MOMENTS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return f"{module.WAKE_NOTE} {module.WAKE_NOTE_ANSWERED}"


def test_parse_reads_the_replay_fields() -> None:
    replay = card_wake.parse(PROMPT)

    assert replay == card_wake.Replay(
        title="Check checkout-gateway's restarts",
        question="Two clusters run checkout-gateway. Which should I look at?",
        options=("seeded-a", "seeded-b"),
        answer="seeded-b",
    )
    # needs_you turns each trailing "- " line into a choice button.
    assert replay.reason == REASON


def test_an_ordinary_prompt_is_not_a_replay() -> None:
    assert card_wake.parse("is checkout-gateway restarting?") is None
    assert card_wake.parse(f"please run this:\n{PROMPT}") is None


@pytest.mark.parametrize("field", ["title", "question", "options", "answer"])
def test_a_replay_missing_a_field_is_an_authoring_error(field: str) -> None:
    prompt = "\n".join(line for line in PROMPT.splitlines() if not line.startswith(f"{field}:"))
    with pytest.raises(ValueError, match=field):
        card_wake.parse(prompt)


def test_a_replay_whose_options_are_blank_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="options"):
        card_wake.parse(PROMPT.replace("seeded-a | seeded-b", " | "))


@needs_moments
def test_with_the_module_the_question_posts_once_and_the_wake_carries_the_note(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(_with_moments(hermes_root), tmp_path)

    planted = _plant(shell)

    assert planted.posted == 1
    assert planted.wake == (
        f"[kanban] Task {planted.card} blocked; needs attention.\n"
        f"Assignee: @{card_wake.WAKE_ASSIGNEE}\n\n{_moments_note()}"
    )
    # The question's choices sit inside the side bar's attachment, so the
    # replay's capture of it is what lets a fresh session read them.
    (side_bar,) = planted.post["attachments"]
    assert side_bar["color"] == "#ECB22E"
    assert "seeded-a" in json.dumps(side_bar["blocks"])
    assert "seeded-a" not in json.dumps(planted.post["blocks"])
    card = _board(tmp_path)["tasks"][planted.card]
    assert card["status"] == "blocked"
    assert card["created_by"] == card_wake.CARD_CREATOR
    assert _board(tmp_path)["events"][0]["payload"] == {"reason": REASON, "kind": "needs_input"}


def test_without_the_module_nothing_posts_and_the_wake_is_plain(
    hermes_root: Path, tmp_path: Path
) -> None:
    """The image on main: the case's red comes from this wake."""
    planted = _plant(_shell_for(hermes_root, tmp_path))

    assert planted.posted == 0
    assert planted.key == KEY
    assert planted.wake == (
        f"[kanban] Task {planted.card} blocked; needs attention.\nAssignee: @{card_wake.WAKE_ASSIGNEE}"
    )
    card = _board(tmp_path)["tasks"][planted.card]
    # Only the notifier's copy is assigned: unblocked, an assigned card is one a worker would claim.
    assert card["assignee"] is None
    assert card["key"] == KEY


def test_a_plant_archives_only_other_runs_stale_replay_cards(
    hermes_root: Path, tmp_path: Path
) -> None:
    """What an archive whose exec failed left behind; a run going on beside this one keeps its card."""
    now = int(time.time())
    stale = now - card_wake.STALE_REPLAY_SECONDS - 60

    def card(key: str, created_at: int) -> dict:
        return {"title": "t", "body": None, "created_by": card_wake.CARD_CREATOR, "status": "blocked",
                "assignee": None, "key": key, "comments": [], "created_at": created_at}

    (tmp_path / "board.json").write_text(json.dumps({"events": [], "tasks": {
        "t_00000001": card(f"{card_wake.REPLAY_KEY_PREFIX}old", stale),
        "t_00000002": card(f"{card_wake.REPLAY_KEY_PREFIX}beside", now),
        "t_00000003": card("someone-elses-card", stale),
    }}))

    planted = _plant(_shell_for(hermes_root, tmp_path))

    tasks = _board(tmp_path)["tasks"]
    assert {tid: row["status"] for tid, row in tasks.items() if tid != planted.card} == {
        "t_00000001": "archived",
        "t_00000002": "blocked",
        "t_00000003": "blocked",
    }
    assert tasks[planted.card]["status"] == "blocked"


def test_a_moments_module_that_posts_nothing_is_a_broken_plant(
    hermes_root: Path, tmp_path: Path
) -> None:
    """The plain wake it would build is the red one, so it must not pass for a red run or infrastructure."""
    (hermes_root / "gateway" / "slack_ux_moments.py").write_text(_SILENT_MOMENTS)

    with pytest.raises(card_wake.ReplayBroken, match="posted nothing"):
        _plant(_shell_for(hermes_root, tmp_path))
    assert [card["status"] for card in _board(tmp_path)["tasks"].values()] == ["archived"]


def test_a_failed_wake_archives_the_card_it_filed(hermes_root: Path, tmp_path: Path) -> None:
    shell = _shell_for(hermes_root, tmp_path, FAKE_WAKE_FAILS="1")

    with pytest.raises(card_wake.ReplayBroken, match="notifier exploded"):
        _plant(shell)

    [card] = _board(tmp_path)["tasks"].values()
    assert card["status"] == "archived"


def test_archive_reads_the_card_as_the_run_left_it_then_archives_it(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(hermes_root, tmp_path)
    planted = _plant(shell)
    _run(
        "import sys; sys.path.insert(0, sys.argv[1]); from hermes_cli import kanban_db as kb;"
        " kb.comment(sys.argv[2], 'default', 'seeded-b')",
        [str(hermes_root), planted.card],
        tmp_path,
    )

    settled = card_wake.archive(shell, KEY, timeout=30)

    assert settled == card_wake.Settled("blocked", ({"author": "default", "body": "seeded-b"},))
    assert settled.archived
    assert _board(tmp_path)["tasks"][planted.card]["status"] == "archived"


def test_an_archive_that_times_out_keeps_the_card_it_read(hermes_root: Path, tmp_path: Path) -> None:
    shell = _shell_for(hermes_root, tmp_path)
    _plant(shell)

    def archive_timed_out(command: str, timeout: float) -> str:
        return "" if command == card_wake.archive_command(KEY) else shell(command, timeout)

    settled = card_wake.archive(archive_timed_out, KEY, timeout=30)

    assert settled == card_wake.Settled("blocked", ())
    assert not settled.archived


def test_no_card_carrying_the_key_reads_as_unknown(hermes_root: Path, tmp_path: Path) -> None:
    """Not as a card with no status, which ``status_not_in`` would pass."""
    shell = _shell_for(hermes_root, tmp_path)
    assert card_wake.archive(shell, KEY, timeout=30) is None


def test_a_plant_whose_exec_gave_out_sweeps_the_card_it_filed(
    hermes_root: Path, tmp_path: Path
) -> None:
    """The script filed the card, then ``kubectl exec`` timed out: the reply is empty."""
    shell = _shell_for(hermes_root, tmp_path)

    def timed_out(command: str, timeout: float) -> str:
        reply = shell(command, timeout)
        return "" if command != card_wake.archive_command(KEY) else reply

    with pytest.raises(card_wake.ReplayUnavailable, match="did not run"):
        _plant(timed_out)
    assert [card["status"] for card in _board(tmp_path)["tasks"].values()] == ["archived"]


@pytest.mark.parametrize(
    ("reply", "error", "reason"),
    [
        ("", card_wake.ReplayUnavailable, "did not run"),
        (f"{card_wake.REPLAY_PRESENT}\nnot json", card_wake.ReplayBroken, "not JSON"),
        (f"{card_wake.REPLAY_PRESENT}\n[]", card_wake.ReplayBroken, "not an object"),
        (f'{card_wake.REPLAY_PRESENT}\n{{"error": "ImportError: no hermes"}}', card_wake.ReplayBroken, "no hermes"),
        (f'{card_wake.REPLAY_PRESENT}\n{{"card": "t_1", "wake": " "}}', card_wake.ReplayBroken, "no wake"),
    ],
)
def test_plant_refuses_a_reply_it_cannot_trust(reply: str, error: type, reason: str) -> None:
    """Only a script that never finished is infrastructure; one that ran and failed is the image's."""
    with pytest.raises(error, match=reason):
        card_wake.plant(lambda command, timeout: reply, card_wake.parse(PROMPT), 30)


def test_archive_is_best_effort() -> None:
    assert card_wake.archive(lambda command, timeout: "", KEY, 30) is None
    read = f'{card_wake.REPLAY_PRESENT}\n{{"card": "t_1", "status": "ready", "comments": [], "error": null}}'
    refused = f'{card_wake.REPLAY_PRESENT}\n{{"archived": false, "error": "OperationalError: locked"}}'

    def shell(command: str, timeout: float) -> str:
        return read if command == card_wake.read_command(KEY) else refused

    settled = card_wake.archive(shell, KEY, 30)
    assert settled == card_wake.Settled("ready", ())
    assert not settled.archived



# --- An answer by button (answer_by: click) ----------------------------------

CLICK_PROMPT = PROMPT + "answer_by: click\n"


def _with_clicks(root: Path) -> Path:
    shutil.copy(CLICKS, root / "gateway" / "slack_ux_clicks.py")
    return _with_moments(root)


def test_parse_reads_an_answer_by_click() -> None:
    assert card_wake.parse(CLICK_PROMPT).clicked
    assert not card_wake.parse(PROMPT).clicked


def test_an_answer_by_anything_but_click_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="answer_by"):
        card_wake.parse(PROMPT + "answer_by: typing\n")


@pytest.mark.parametrize("line", ["Answer_by: click", "answer-by: click", "answer by click"])
def test_a_line_that_is_not_a_field_is_an_authoring_error(line: str) -> None:
    with pytest.raises(ValueError, match="is not one of"):
        card_wake.parse(PROMPT + line + "\n")


def test_an_empty_answer_by_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="answer_by"):
        card_wake.parse(PROMPT + "answer_by:\n")


def test_an_empty_session_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="session"):
        card_wake.parse(PROMPT + "session:\n")


def test_a_click_answer_that_is_not_an_option_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="options a click can press"):
        card_wake.parse(CLICK_PROMPT.replace("answer: seeded-b", "answer: seeded-c"))


@needs_moments
@pytest.mark.skipif(not CLICKS.exists(), reason="main has no deploy/docker/patches/slack_ux_clicks.py")
def test_a_click_plant_builds_the_turn_the_images_button_sends(hermes_root: Path, tmp_path: Path) -> None:
    planted = _plant(_shell_for(_with_clicks(hermes_root), tmp_path, CLICK_PROMPT), CLICK_PROMPT)

    assert planted.click is not None and planted.click.startswith("seeded-b")
    assert planted.card in planted.click
    assert planted.answer(card_wake.parse(CLICK_PROMPT)) == planted.click
    assert planted.answer(card_wake.parse(PROMPT)) == "seeded-b"


@needs_moments
@pytest.mark.skipif(not CLICKS.exists(), reason="main has no deploy/docker/patches/slack_ux_clicks.py")
@pytest.mark.parametrize("option", ["[seeded-b](https://example.com/b)", "`seeded-b`", "**seeded-b**"])
def test_a_click_plant_presses_a_marked_up_option_by_the_label_its_button_shows(
    hermes_root: Path, tmp_path: Path, option: str
) -> None:
    prompt = CLICK_PROMPT.replace("| seeded-b", f"| {option}").replace("answer: seeded-b", f"answer: {option}")

    planted = _plant(_shell_for(_with_clicks(hermes_root), tmp_path, prompt), prompt)

    assert planted.click is not None and planted.click.startswith("seeded-b")


@needs_moments
@pytest.mark.skipif(not CLICKS.exists(), reason="main has no deploy/docker/patches/slack_ux_clicks.py")
def test_a_click_plant_on_a_question_shown_without_buttons_names_the_case(hermes_root: Path, tmp_path: Path) -> None:
    # No question mark before the options, so slack_moments leaves them as text.
    prompt = CLICK_PROMPT.replace("Which should I look at?", "Pick one to look at.")

    with pytest.raises(card_wake.ReplayBroken, match="shows no buttons"):
        _plant(_shell_for(_with_clicks(hermes_root), tmp_path, prompt), prompt)


@needs_moments
def test_a_click_plant_on_an_image_without_the_clicks_module_is_broken(hermes_root: Path, tmp_path: Path) -> None:
    with pytest.raises(card_wake.ReplayBroken, match="slack_ux_clicks"):
        _plant(_shell_for(_with_moments(hermes_root), tmp_path, CLICK_PROMPT), CLICK_PROMPT)
    assert [card["status"] for card in _board(tmp_path)["tasks"].values()] == ["archived"]


def test_a_click_plant_on_an_image_without_moments_is_broken(hermes_root: Path, tmp_path: Path) -> None:
    with pytest.raises(card_wake.ReplayBroken, match="slack_ux_moments"):
        _plant(_shell_for(hermes_root, tmp_path, CLICK_PROMPT), CLICK_PROMPT)


def test_a_click_plant_that_built_no_turn_is_broken() -> None:
    reply = f'{card_wake.REPLAY_PRESENT}\n{{"card": "t_1", "wake": "[kanban] Task t_1 blocked.", "click": null}}'
    with pytest.raises(card_wake.ReplayBroken, match="no click turn"):
        card_wake.plant(lambda command, timeout: reply, card_wake.parse(CLICK_PROMPT), 30)


def test_merge_carries_the_answer_reply_for_the_verifier() -> None:
    merged = card_wake.merge(_PLANTED, _result("[SILENT]", [], {}), _result("Unblocked it.", [], {}))

    assert merged.trajectory[-1]["args"]["answer_reply"] == "Unblocked it."
    assert merged.metadata["final_message"] == "[SILENT]"


def test_merge_records_no_answer_reply_for_an_errored_answer_turn() -> None:
    answer = AgentResult.errored("HTTP 500 from agent endpoint: upstream")

    merged = card_wake.merge(_PLANTED, _result("[SILENT]", [], {}), answer)

    assert "answer_reply" not in merged.trajectory[-1]["args"]


def test_merge_keeps_the_reply_of_an_answer_turn_that_parsed_with_warnings() -> None:
    # No tool call parsed, so only final_message says the turn replied.
    answer = AgentResult(
        output="[SILENT]",
        trajectory=[],
        errors=["tool output without a matching call"],
        metadata={"final_message": "[SILENT]"},
    )

    merged = card_wake.merge(_PLANTED, _result("[SILENT]", [], {}), answer)

    assert merged.trajectory[-1]["args"]["answer_reply"] == "[SILENT]"


# --- A typed answer in a fresh session (session: fresh) -----------------------

FRESH_PROMPT = PROMPT + "session: fresh\n"

# A stand-in for the image's Slack adapter: the formatter's contract, not its rendering.
_FAKE_SLACK_ADAPTER = """
class SlackAdapter:
    async def _format_thread_context(self, messages, *, thread_ts, current_ts, team_id, channel_id):
        assert self._is_sender_authorized() and channel_id and thread_ts
        bot = self._team_bot_user_ids.get(team_id, self._bot_user_id)
        lines = []
        for msg in messages:
            if msg.get("user") == bot:
                lines.append("[assistant] " + msg["text"])
            else:
                lines.append(await self._resolve_user_name(msg["user"]) + ": " + msg["text"])
        if not lines:
            return "", ""
        return "[Thread context]\\n" + "\\n".join(lines) + "\\n[End of thread context]\\n\\n", lines[0]
"""


def _with_slack_adapter(root: Path) -> Path:
    adapter = root / "plugins" / "platforms" / "slack"
    adapter.mkdir(parents=True)
    (adapter / "adapter.py").write_text(_FAKE_SLACK_ADAPTER)
    return root


def _context_shell(root: Path, tmp_path: Path):
    def shell(command: str, timeout: float) -> str:
        tokens = shlex.split(command)
        assert tokens[tokens.index("-c") + 1] == card_wake._CONTEXT_SCRIPT
        args = tokens[tokens.index("-c") + 2 :]
        args[1], args[2] = str(root), str(SCRIPTS)
        return _run(card_wake._CONTEXT_SCRIPT, args, tmp_path)

    return shell


def test_parse_reads_a_fresh_session() -> None:
    assert card_wake.parse(FRESH_PROMPT).fresh
    assert not card_wake.parse(PROMPT).fresh


def test_a_session_other_than_fresh_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="session"):
        card_wake.parse(PROMPT + "session: same\n")


def test_a_fresh_plant_files_a_decoy_that_lists_first_and_reads_newer(
    hermes_root: Path, tmp_path: Path
) -> None:
    planted = _plant(_shell_for(hermes_root, tmp_path, FRESH_PROMPT), FRESH_PROMPT)

    tasks = _board(tmp_path)["tasks"]
    decoy, card = tasks[planted.decoy], tasks[planted.card]
    assert decoy["key"] == card_wake.decoy_key(KEY)
    assert decoy["status"] == "blocked"
    assert decoy["priority"] > card["priority"]
    assert card["backdated"] > 0 and "backdated" not in decoy
    assert (decoy["title"], decoy["body"]) == (card["title"], card["body"])
    blocks = [e["payload"] for e in _board(tmp_path)["events"] if e["task_id"] == planted.decoy]
    assert blocks == [{"reason": REASON, "kind": "needs_input"}]


def test_an_ordinary_plant_files_no_decoy(hermes_root: Path, tmp_path: Path) -> None:
    planted = _plant(_shell_for(hermes_root, tmp_path))

    assert planted.decoy is None
    [card] = _board(tmp_path)["tasks"].values()
    assert "backdated" not in card


def test_archive_reads_the_decoy_beside_the_card_and_archives_both(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(hermes_root, tmp_path, FRESH_PROMPT)
    _plant(shell, FRESH_PROMPT)

    settled = card_wake.archive(shell, KEY, timeout=30)

    assert settled == card_wake.Settled("blocked", (), decoy_status="blocked")
    assert settled.as_metadata()["decoy_status"] == "blocked"
    assert {t["status"] for t in _board(tmp_path)["tasks"].values()} == {"archived"}


def test_a_decoy_the_agent_archived_reads_as_archived(hermes_root: Path, tmp_path: Path) -> None:
    """An agent mistake, so a fail for ``decoy_status_in``, not a card that could not be read."""
    shell = _shell_for(hermes_root, tmp_path, FRESH_PROMPT)
    planted = _plant(shell, FRESH_PROMPT)
    _run(
        "import sys; sys.path.insert(0, sys.argv[1]); from hermes_cli import kanban_db as kb;"
        " kb.archive_task(None, sys.argv[2])",
        [str(hermes_root), planted.decoy],
        tmp_path,
    )

    assert card_wake.archive(shell, KEY, timeout=30).decoy_status == "archived"


def test_a_fresh_plant_that_filed_no_decoy_is_broken() -> None:
    swept: list[str] = []
    planted = json.dumps({"card": "t_1", "decoy": None, "wake": "[kanban] Task t_1 blocked.", "posted": 0})

    def shell(command: str, timeout: float) -> str:
        if "create_task" in command:
            return f"{card_wake.REPLAY_PRESENT}\n{planted}"
        swept.append(command)
        return f'{card_wake.REPLAY_PRESENT}\n{{"archived": true, "cards": [], "error": null}}'

    with pytest.raises(card_wake.ReplayBroken, match="decoy"):
        card_wake.plant(shell, card_wake.parse(FRESH_PROMPT), timeout=30, key=KEY)
    assert swept == [card_wake.archive_command(KEY)]


def test_the_thread_holds_the_ask_the_question_and_a_posted_wake_reply() -> None:
    replay = card_wake.parse(FRESH_PROMPT)
    # The shape the plant captures: the question's detail and choices sit in
    # the side bar's attachment, not in the top-level blocks.
    choices = [{"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "seeded-a"}}]}]
    post = {
        "text": "Which should I look at? (Question from card t_1.)",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*Which should I look at?*"}}],
        "attachments": [{"color": "#ECB22E", "blocks": choices}],
    }
    planted = card_wake.Planted("t_1", "wake", 1, KEY, post, "t_2")

    messages = card_wake.thread_messages(replay, planted, "seeded-a or seeded-b?")

    assert [(m["user"], m["text"]) for m in messages] == [
        (card_wake.STUB_USER, replay.title),
        (card_wake.STUB_BOT, post["text"]),
        (card_wake.STUB_BOT, "seeded-a or seeded-b?"),
    ]
    assert (messages[1]["blocks"], messages[1]["attachments"]) == (post["blocks"], post["attachments"])
    assert [m["ts"] for m in messages] == sorted(m["ts"] for m in messages)


def test_the_thread_leaves_out_a_silent_wake_reply_and_a_post_the_image_never_made() -> None:
    replay = card_wake.parse(FRESH_PROMPT)
    planted = card_wake.Planted("t_1", "wake", 0, KEY, None, "t_2")

    assert [m["text"] for m in card_wake.thread_messages(replay, planted, "")] == [replay.title]


def test_the_fresh_answer_is_the_adapters_thread_context_then_the_attributed_answer(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _context_shell(_with_slack_adapter(hermes_root), tmp_path)
    replay = card_wake.parse(FRESH_PROMPT)
    post = {"text": "Which should I look at? (Question from card t_1.)", "blocks": []}
    messages = card_wake.thread_messages(replay, card_wake.Planted("t_1", "w", 1, KEY, post, "t_2"), "")

    prompt = card_wake.fresh_answer(shell, messages, "seeded-b", timeout=30)

    assert prompt == (
        f"[Thread context]\n{card_wake.STUB_USER_NAME}: {replay.title}\n[assistant] {post['text']}\n"
        "[End of thread context]\n\n\n\n[New message]\n"
        f"[{card_wake.STUB_USER_NAME} | Slack user <@{card_wake.STUB_USER}>] seeded-b"
    )


def test_an_image_without_the_slack_adapter_breaks_the_fresh_answer(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _context_shell(hermes_root, tmp_path)
    messages = card_wake.thread_messages(card_wake.parse(FRESH_PROMPT), card_wake.Planted("t_1", "w", 0), "")

    with pytest.raises(card_wake.ReplayBroken, match="ModuleNotFoundError"):
        card_wake.fresh_answer(shell, messages, "seeded-b", timeout=30)


def test_a_fresh_answer_whose_exec_gave_out_is_infrastructure() -> None:
    with pytest.raises(card_wake.ReplayUnavailable):
        card_wake.fresh_answer(lambda command, timeout: "", [], "seeded-b", timeout=30)

def test_an_unblock_in_the_wake_turn_leaves_the_blocked_card_ready_for_no_worker(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(hermes_root, tmp_path)
    planted = _plant(shell)

    # What a kanban_unblock from the woken turn does to the board's card.
    _run(
        "import sys; sys.path.insert(0, sys.argv[1]);"
        " from hermes_cli import kanban_db as kb;"
        " assert kb.unblock_task(kb.connect(), sys.argv[2])",
        [str(hermes_root), planted.card],
        tmp_path,
    )

    board = _board(tmp_path)
    card = board["tasks"][planted.card]
    # Ready with no assignee: the dispatcher files that under skipped_unassigned
    # and spawns nothing, so no real worker picks the replayed card up.
    assert card["status"] == "ready"
    assert card["assignee"] is None
    assert [e["kind"] for e in board["events"]] == ["blocked", "unblocked"]


_PLANTED = card_wake.Planted(card="t_1", wake="[kanban] Task t_1 blocked.", posted=1)


def _result(output: str, names: list[str], tokens: dict, **metadata: object) -> AgentResult:
    return AgentResult(
        output=output,
        trajectory=[{"name": n, "args": {}, "result": "{}", "status": "completed"} for n in names],
        tokens=tokens,
        metadata=dict(metadata),
    )


def test_merge_grades_the_wake_reply_and_keeps_both_trajectories() -> None:
    wake = _result("[SILENT]", [], {"input": 10, "output": 1, "total": 11}, session_id="s")
    answer = _result(
        "Passed seeded-b to the card.",
        ["kanban_comment", "kanban_unblock"],
        {"input": 30, "output": 5, "total": 35},
        session_id="s",
    )

    merged = card_wake.merge(_PLANTED, wake, answer)

    assert merged.output == "[SILENT]"
    assert merged.metadata["final_message"] == "[SILENT]"
    assert [step["name"] for step in merged.trajectory] == [
        "kanban_comment",
        "kanban_unblock",
        card_wake.SETTLED_ENTRY,
    ]
    # One session: the answer turn's row already counts the wake turn.
    assert merged.tokens == {"input": 30, "output": 5, "total": 35}
    assert merged.metadata["question_wake"]["answer_output"] == "Passed seeded-b to the card."
    assert merged.metadata["question_wake"]["posted"] == 1


def test_merge_sums_tokens_across_sessions_and_keeps_an_empty_reply_empty() -> None:
    wake = _result("", ["kanban_show"], {"input": 10, "output": None, "total": 10})
    answer = _result("done", [], {"input": 30, "output": 5, "total": 35}, session_id="s2")

    merged = card_wake.merge(_PLANTED, wake, answer)

    assert merged.tokens == {"input": 40, "output": 5, "total": 45}
    assert merged.output == ""
    assert merged.metadata["final_message"] == ""


def test_merge_keeps_both_turns_errors() -> None:
    wake = AgentResult(output="a", trajectory=[], errors=["first"], metadata={"final_message": "a"})
    answer = AgentResult(
        output="b", trajectory=[], errors=["second"], metadata={"final_message": "b"}
    )

    assert card_wake.merge(_PLANTED, wake, answer).errors == ["first", "second"]


def test_merge_leads_with_an_answer_turn_that_failed_after_a_wake_that_warned() -> None:
    # Scoring reads only errors[0], so the wake's warning would hide the answer's infra marker.
    wake = AgentResult(
        output="[SILENT]",
        trajectory=[],
        errors=["skipped an item"],
        metadata={"final_message": "[SILENT]"},
    )
    answer = AgentResult.errored("KUBE_AGENTS_INFRA_FAILURE: 429")

    errors = card_wake.merge(_PLANTED, wake, answer).errors

    assert errors == ["KUBE_AGENTS_INFRA_FAILURE: 429", "skipped an item"]


def test_merge_records_the_card_as_the_run_left_it() -> None:
    settled = card_wake.Settled("ready", ({"author": "default", "body": "seeded-b"},))

    merged = card_wake.merge(_PLANTED, _result("", [], {}), _result("", [], {}), settled)

    assert merged.metadata["question_wake"]["settled"] == {
        "status": "ready",
        "comments": [{"author": "default", "body": "seeded-b"}],
    }
    unknown = card_wake.merge(_PLANTED, _result("", [], {}), _result("", [], {}))
    assert unknown.metadata["question_wake"]["settled"] is None


def test_merge_sums_nested_token_buckets_across_sessions() -> None:
    wake = _result("", [], {"input": 10, "workers": {"input": 3, "output": None}}, session_id="s1")
    answer = _result("done", [], {"input": 30, "workers": {"input": 4, "output": 2}}, session_id="s2")

    merged = card_wake.merge(_PLANTED, wake, answer)

    assert merged.tokens == {"input": 40, "workers": {"input": 7, "output": 2}}


def test_merge_keeps_the_worker_capture_of_the_turn_that_delegated() -> None:
    wake = _result("[SILENT]", [], {}, worker_commands=None, worker_trajectory=None)
    capture = {"sessions": {"t_2": "w1"}, "unread": []}
    answer = _result(
        "done", ["kanban_create"], {}, worker_commands=[{"cmd": "kubectl get pods"}], worker_trajectory=capture
    )

    merged = card_wake.merge(_PLANTED, wake, answer)

    assert merged.metadata["worker_commands"] == [{"cmd": "kubectl get pods"}]
    assert merged.metadata["worker_trajectory"] == capture


def test_merge_combines_two_worker_captures() -> None:
    wake = _result(
        "", [], {}, worker_commands=[{"cmd": "a"}], worker_trajectory={"sessions": {"t_1": "w1"}, "unread": ["x"]}
    )
    answer = _result(
        "", [], {}, worker_commands=[{"cmd": "b"}], worker_trajectory={"sessions": {"t_2": "w2"}, "unread": ["y"]}
    )

    merged = card_wake.merge(_PLANTED, wake, answer)

    assert merged.metadata["worker_commands"] == [{"cmd": "a"}, {"cmd": "b"}]
    assert merged.metadata["worker_trajectory"] == {"sessions": {"t_1": "w1", "t_2": "w2"}, "unread": ["x", "y"]}


def test_merge_keeps_the_wake_turns_workers_on_one_session() -> None:
    wake = _result("", [], {"input": 13, "workers": {"input": 3}}, session_id="s1")
    answer = _result("done", [], {"input": 54, "workers": {"input": 4}}, session_id="s1")

    merged = card_wake.merge(_PLANTED, wake, answer)

    assert merged.tokens == {"input": 57, "workers": {"input": 7}}


def test_tag_keeps_the_wake_reply_and_records_the_card() -> None:
    wake = _result(
        "I couldn't restart checkout-gateway on seeded-a.",
        ["kanban_show"],
        {"input": 10, "output": 4, "total": 14},
        session_id="s",
        final_message="I couldn't restart checkout-gateway on seeded-a.",
    )

    tagged = card_wake.tag(_PLANTED, wake)

    assert tagged.output == wake.output
    assert tagged.metadata["final_message"] == "I couldn't restart checkout-gateway on seeded-a."
    assert tagged.trajectory == [
        *wake.trajectory,
        {"name": card_wake.SETTLED_ENTRY, "args": {"card": "t_1"}, "result": None, "status": "harness"},
    ]
    assert tagged.tokens == wake.tokens
    assert tagged.metadata["question_wake"] == {
        "card": "t_1",
        "wake": "[kanban] Task t_1 blocked.",
        "posted": 1,
        "settled": None,
    }


def test_the_settled_card_rides_on_the_trajectory_for_the_verifier() -> None:
    settled = card_wake.Settled("ready", ({"author": "default", "body": "seeded-b"},))
    merged = card_wake.merge(_PLANTED, _result("", [], {}), _result("", [], {}), settled)

    assert merged.trajectory[-1] == {
        "name": card_wake.SETTLED_ENTRY,
        "args": {"card": "t_1", "answer_reply": ""},
        "result": {"status": "ready", "comments": [{"author": "default", "body": "seeded-b"}]},
        "status": "harness",
    }


# --- Block replays: a card blocked on something other than a question ---------

BLOCK_PROMPT = """[bench:card-block-wake]
title: Health check for checkout-gateway on seeded-a
kind: transient
reason: provider rate limit: API retries exhausted (failure_reason=rate_limit): HTTP 429
"""
BLOCK_REASON = "provider rate limit: API retries exhausted (failure_reason=rate_limit): HTTP 429"


def test_parse_reads_a_block_replay() -> None:
    replay = card_wake.parse(BLOCK_PROMPT)

    assert replay == card_wake.Replay(
        title="Health check for checkout-gateway on seeded-a",
        question="",
        options=(),
        answer="",
        kind="transient",
        block_reason=BLOCK_REASON,
    )
    assert replay.reason == BLOCK_REASON
    # The card's body is the work, not the failure.
    assert replay.body == "Health check for checkout-gateway on seeded-a"
    assert not replay.answered
    assert card_wake.parse(PROMPT).answered


@pytest.mark.parametrize("field", ["title", "kind", "reason"])
def test_a_block_replay_missing_a_field_is_an_authoring_error(field: str) -> None:
    prompt = "\n".join(line for line in BLOCK_PROMPT.splitlines() if not line.startswith(f"{field}:"))
    with pytest.raises(ValueError, match=field):
        card_wake.parse(prompt)


@pytest.mark.parametrize("kind", ["needs_input", "rate_limit"])
def test_a_block_replay_of_a_question_or_an_unknown_kind_is_an_authoring_error(kind: str) -> None:
    with pytest.raises(ValueError, match="kind"):
        card_wake.parse(BLOCK_PROMPT.replace("kind: transient", f"kind: {kind}"))


def test_a_question_field_in_a_block_replay_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="answer"):
        card_wake.parse(BLOCK_PROMPT + "answer: seeded-b\n")


def test_a_block_plant_files_the_kind_and_reason_and_posts_nothing(hermes_root: Path, tmp_path: Path) -> None:
    planted = _plant(_shell_for(hermes_root, tmp_path, BLOCK_PROMPT), BLOCK_PROMPT)

    assert planted.posted == 0
    assert planted.wake == (
        f"[kanban] Task {planted.card} blocked; needs attention.\nAssignee: @{card_wake.WAKE_ASSIGNEE}"
    )
    card = _board(tmp_path)["tasks"][planted.card]
    assert card["status"] == "blocked"
    assert card["body"] == "Health check for checkout-gateway on seeded-a"
    assert _board(tmp_path)["events"][0]["payload"] == {"reason": BLOCK_REASON, "kind": "transient"}


@needs_moments
def test_a_block_plant_on_an_image_with_moments_is_not_broken_for_posting_nothing(
    hermes_root: Path, tmp_path: Path
) -> None:
    """needs_you posts only a question; a transient block posting nothing is the image working."""
    shell = _shell_for(_with_moments(hermes_root), tmp_path, BLOCK_PROMPT)

    planted = _plant(shell, BLOCK_PROMPT)

    assert planted.posted == 0
    assert planted.post is None
    assert _board(tmp_path)["tasks"][planted.card]["status"] == "blocked"
