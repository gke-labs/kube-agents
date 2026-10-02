"""The card-wake replays: their prompts, their in-pod scripts, and how the turns become one result.

The in-pod scripts run here under the test interpreter, the way
``test_board.py`` runs its sibling, against a stand-in hermes tree: a
``kanban_db`` that keeps its board in a JSON file, a dispatcher whose failure
breaker counts and trips as the real one does, and a notifier whose wake
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
from pathlib import Path

import pytest
import yaml
from devops_bench.agents import AgentResult
from kube_agents_bench import card_wake

REPO = Path(__file__).resolve().parents[2]
MOMENTS = REPO / "deploy" / "docker" / "patches" / "slack_ux_moments.py"
SCRIPTS = REPO / "agents" / "platform" / "scripts"
WORKER_CASES = {
    "chat-voice-retry-says-it-is-retried": card_wake.OUTCOME_CRASHED,
    "chat-voice-final-attempt-is-not-retried": card_wake.OUTCOME_TIMED_OUT_FINAL,
}
FAILURE_CASE = REPO / "bench" / "tasks" / "chat-voice-failure-leads-with-fact" / "task.yaml"

PROMPT = """[bench:slack-question-wake]
title: Check checkout-gateway's restarts
question: Two clusters run checkout-gateway. Which should I look at?
options: seeded-a | seeded-b
answer: seeded-b
"""
REASON = "Two clusters run checkout-gateway. Which should I look at?\n- seeded-a\n- seeded-b"
FAILURE_PROMPT = """[bench:card-failure-wake]
title: Restart checkout-gateway on seeded-a
body: Roll the checkout-gateway Deployment on seeded-a.
outcome: blocked
reason: Permission denied: container.deployments.update on seeded-a.
"""
FAILURE_REASON = "Permission denied: container.deployments.update on seeded-a."
needs_moments = pytest.mark.skipif(
    not MOMENTS.exists(), reason="main has no deploy/docker/patches/slack_ux_moments.py"
)

_FAKE_KANBAN_DB = '''
import contextlib, json, os
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
    # The one query the archive script makes: a key's live cards, newest first.
    def execute(self, sql, params):
        assert "idempotency_key" in sql, sql
        tasks = _load()["tasks"]
        return [(tid,) for tid in sorted(tasks, reverse=True)
                if tasks[tid]["key"] == params[0] and tasks[tid]["status"] != "archived"]


def connect():
    return _Conn()


def create_task(conn, *, title, body=None, created_by=None, idempotency_key=None):
    state = _load()
    task_id = "t_%08d" % (len(state["tasks"]) + 1)
    state["tasks"][task_id] = {"title": title, "body": body, "created_by": created_by,
                               "status": "ready", "assignee": None, "key": idempotency_key,
                               "comments": [], "failures": 0}
    _save(state)
    return task_id


def comment(task_id, author, body):
    state = _load()
    state["tasks"][task_id]["comments"].append({"author": author, "body": body})
    _save(state)


def list_comments(conn, task_id):
    return [SimpleNamespace(**c) for c in _load()["tasks"][task_id]["comments"]]


@contextlib.contextmanager
def write_txn(conn):
    yield conn


def _append_event(conn, task_id, kind, payload=None):
    state = _load()
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id, "kind": kind,
                            "payload": payload})
    _save(state)


def assign_task(conn, task_id, profile):
    state = _load()
    task = state["tasks"][task_id]
    if task["assignee"] != profile:
        # As the real one: a new assignee starts a fresh failure streak.
        task["failures"], task["last_failure_error"] = 0, None
    task["assignee"] = profile
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id, "kind": "assigned",
                            "payload": {"assignee": profile}})
    _save(state)
    return True


def block_task(conn, task_id, *, reason=None, kind=None):
    state = _load()
    state["tasks"][task_id]["status"] = "blocked"
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id, "kind": "blocked",
                            "payload": {"reason": reason, "kind": kind}})
    _save(state)
    return True


def unblock_task(conn, task_id):
    # As the real one: back to ready with a fresh failure count; the assignee is untouched.
    state = _load()
    task = state["tasks"][task_id]
    if task["status"] != "blocked":
        return False
    task["status"], task["failures"], task["last_failure_error"] = "ready", 0, None
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

_FAKE_DISPATCH = '''
import os

from hermes_cli.kanban_db import _load, _save

DEFAULT_FAILURE_LIMIT = int(os.environ.get("FAKE_FAILURE_LIMIT", "2"))


def _record_task_failure(conn, task_id, error, *, outcome, force_trip=False,
                         event_payload_extra=None):
    state = _load()
    task = state["tasks"][task_id]
    task["failures"] += 1
    task["last_failure_error"] = error
    if not (force_trip or task["failures"] >= DEFAULT_FAILURE_LIMIT):
        _save(state)
        return False
    task["status"] = "blocked"
    payload = {"error": error, "trigger_outcome": outcome, "force_trip": force_trip,
               **(event_payload_extra or {})}
    state["events"].append({"id": len(state["events"]) + 1, "task_id": task_id, "kind": "gave_up",
                            "payload": payload})
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


# The pinned locale's wake statuses, joined in upstream's _WAKE_KINDS order.
_STATUS = {
    "gave_up": "gave up (retries exhausted)",
    "crashed": "crashed (worker exited); dispatcher will retry",
    "timed_out": "timed out; dispatcher will retry",
    "blocked": "blocked; needs attention",
}


class _KanbanNotification:
    def __init__(self, runner, d, *, platform_cls, sub_fail_counts):
        self.d, self.sub = d, d["sub"]
        self.adapter, self.synth, self.wake_kinds = None, "", set()
        self.is_push_adapter = True

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
        if not self.is_push_adapter:
            self.synth += "\\nVia: %s" % self.sub["platform"]
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
    (root / "hermes_cli" / "kanban_db_dispatch.py").write_text(_FAKE_DISPATCH)
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
    return module.WAKE_NOTE


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


def test_parse_reads_a_failure_prompt() -> None:
    assert card_wake.parse(FAILURE_PROMPT) == card_wake.Failure(
        title="Restart checkout-gateway on seeded-a",
        body="Roll the checkout-gateway Deployment on seeded-a.",
        outcome="blocked",
        reason=FAILURE_REASON,
    )


@pytest.mark.parametrize("field", ["title", "body", "outcome", "reason"])
def test_a_failure_prompt_missing_a_field_is_an_authoring_error(field: str) -> None:
    prompt = "\n".join(
        line for line in FAILURE_PROMPT.splitlines() if not line.startswith(f"{field}:")
    )
    with pytest.raises(ValueError, match=field):
        card_wake.parse(prompt)


@pytest.mark.parametrize(("case", "outcome"), WORKER_CASES.items())
def test_the_worker_failure_case_prompts_are_worker_replays(case: str, outcome: str) -> None:
    task = REPO / "bench" / "tasks" / case / "task.yaml"
    replay = card_wake.parse(yaml.safe_load(task.read_text())["prompt"])

    assert isinstance(replay, card_wake.Failure)
    assert replay.outcome == outcome
    assert "invoice-renderer" in replay.title


def test_the_failure_case_prompt_is_a_blocked_replay() -> None:
    replay = card_wake.parse(yaml.safe_load(FAILURE_CASE.read_text())["prompt"])

    assert isinstance(replay, card_wake.Failure)
    assert replay.outcome == card_wake.OUTCOME_BLOCKED
    assert "invoice-renderer" in replay.title
    assert replay.reason.startswith("Permission denied.")


def test_the_failure_case_says_why_only_from_the_reason() -> None:
    case = yaml.safe_load(FAILURE_CASE.read_text())
    replay = card_wake.parse(case["prompt"])
    (says_why,) = [e for e in case["verification_spec"] if e["name"] == "the-reply-says-why"]
    phrases = says_why["check"]["any_of_phrases"]

    # The wake carries the title, so a phrase it also holds is answerable unread.
    assert all(p in replay.reason for p in phrases)
    assert not any(p in f"{replay.title} {replay.body}" for p in phrases)


def test_a_failure_prompt_with_an_unknown_outcome_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="'completed' is not one of blocked, gave_up, crashed"):
        card_wake.parse(FAILURE_PROMPT.replace("outcome: blocked", "outcome: completed"))


@pytest.mark.parametrize("outcome", card_wake.WORKER_OUTCOMES)
def test_a_failure_prompt_reads_a_worker_outcome(outcome: str) -> None:
    replay = card_wake.parse(FAILURE_PROMPT.replace("outcome: blocked", f"outcome: {outcome}"))

    assert isinstance(replay, card_wake.Failure)
    assert replay.outcome == outcome
    assert card_wake.plant_command(replay, KEY).endswith(f" {outcome} {card_wake.WORKER_ASSIGNEE} {KEY}")


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("crashed", "crashed (worker exited); dispatcher will retry"),
        ("timed_out", "timed out; dispatcher will retry"),
    ],
)
def test_a_retried_worker_failure_wakes_alone_and_leaves_the_card_ready(
    hermes_root: Path, tmp_path: Path, outcome: str, status: str
) -> None:
    prompt = FAILURE_PROMPT.replace("outcome: blocked", f"outcome: {outcome}")
    shell = _shell_for(hermes_root, tmp_path, prompt)

    planted = _plant(shell, prompt)

    assert planted.wake == (
        f"[kanban] Task {planted.card} {status}.\n"
        f"Assignee: @{card_wake.WORKER_ASSIGNEE}\nVia: api_server"
    )
    board = _board(tmp_path)
    card = board["tasks"][planted.card]
    assert card["status"] == "ready"
    # Assigned before it failed, so the count and the error kanban_show reads survive.
    assert (card["failures"], card["last_failure_error"]) == (1, FAILURE_REASON)
    assert [e["kind"] for e in board["events"]] == ["assigned", outcome]
    assert board["events"][1]["payload"]["retry_status"] == "ready"


@pytest.mark.parametrize(
    ("outcome", "trigger", "status"),
    [
        ("crashed_final", "crashed", "crashed (worker exited); dispatcher will retry"),
        ("timed_out_final", "timed_out", "timed out; dispatcher will retry"),
    ],
)
def test_a_final_worker_failure_wakes_with_its_gave_up(
    hermes_root: Path, tmp_path: Path, outcome: str, trigger: str, status: str
) -> None:
    prompt = FAILURE_PROMPT.replace("outcome: blocked", f"outcome: {outcome}")
    shell = _shell_for(hermes_root, tmp_path, prompt)

    planted = _plant(shell, prompt)

    # One wake, both kinds: the breaker's gave_up and the attempt's own
    # "dispatcher will retry".
    assert planted.wake.startswith(
        f"[kanban] Task {planted.card} gave up (retries exhausted), {status}.\n"
    )
    board = _board(tmp_path)
    card = board["tasks"][planted.card]
    assert (card["status"], card["failures"]) == ("blocked", 2)
    assert [e["kind"] for e in board["events"]] == ["assigned", trigger, trigger, "gave_up"]
    gave_up = board["events"][-1]["payload"]
    assert (gave_up["error"], gave_up["trigger_outcome"], gave_up["force_trip"]) == (
        FAILURE_REASON,
        trigger,
        False,
    )


def test_a_final_worker_failure_follows_the_images_failure_limit(
    hermes_root: Path, tmp_path: Path
) -> None:
    prompt = FAILURE_PROMPT.replace("outcome: blocked", "outcome: timed_out_final")
    shell = _shell_for(hermes_root, tmp_path, prompt, FAKE_FAILURE_LIMIT="3")

    planted = _plant(shell, prompt)

    assert planted.wake.startswith(
        f"[kanban] Task {planted.card} gave up (retries exhausted), timed out; dispatcher will retry.\n"
    )
    board = _board(tmp_path)
    assert board["tasks"][planted.card]["failures"] == 3
    assert [e["kind"] for e in board["events"]] == ["assigned"] + ["timed_out"] * 3 + ["gave_up"]


def test_a_worker_failure_the_breaker_disagrees_with_is_a_mismatch_and_archives_the_card(
    hermes_root: Path, tmp_path: Path
) -> None:
    prompt = FAILURE_PROMPT.replace("outcome: blocked", "outcome: crashed")
    shell = _shell_for(hermes_root, tmp_path, prompt, FAKE_FAILURE_LIMIT="1")

    # A mismatch, not ReplayUnavailable: the harness records it as errored, so
    # the case reds rather than being excluded as infrastructure.
    with pytest.raises(card_wake.ReplayMismatch, match="tripped its failure breaker"):
        _plant(shell, prompt)

    [card] = _board(tmp_path)["tasks"].values()
    assert card["status"] == "archived"


def test_a_blocked_failure_wakes_through_the_api_server_with_only_the_wake_assigned(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(hermes_root, tmp_path, FAILURE_PROMPT)

    planted = _plant(shell, FAILURE_PROMPT)

    assert planted.posted == 0
    assert planted.wake == (
        f"[kanban] Task {planted.card} blocked; needs attention.\n"
        f"Assignee: @{card_wake.WAKE_ASSIGNEE}\nVia: api_server"
    )
    board = _board(tmp_path)
    card = board["tasks"][planted.card]
    assert card["status"] == "blocked"
    assert card["body"] == "Roll the checkout-gateway Deployment on seeded-a."
    # Only the notifier's copy is assigned, so an unblock hands no worker the card.
    assert card["assignee"] is None
    assert [e["kind"] for e in board["events"]] == ["blocked"]
    assert board["events"][0]["payload"] == {"reason": FAILURE_REASON, "kind": None}


def test_an_unblock_in_the_wake_turn_leaves_the_blocked_card_ready_for_no_worker(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(hermes_root, tmp_path, FAILURE_PROMPT)
    planted = _plant(shell, FAILURE_PROMPT)

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


def test_a_gave_up_failure_trips_the_breaker_with_the_reason_as_its_error(
    hermes_root: Path, tmp_path: Path
) -> None:
    prompt = FAILURE_PROMPT.replace("outcome: blocked", "outcome: gave_up")
    shell = _shell_for(hermes_root, tmp_path, prompt)

    planted = _plant(shell, prompt)

    assert planted.wake == (
        f"[kanban] Task {planted.card} gave up (retries exhausted).\n"
        f"Assignee: @{card_wake.WAKE_ASSIGNEE}\nVia: api_server"
    )
    board = _board(tmp_path)
    # Never assigned: an unblock or a reset failure count hands no worker the card.
    assert board["tasks"][planted.card]["assignee"] is None
    assert [e["kind"] for e in board["events"]] == ["gave_up"]
    assert board["events"][0]["payload"] == {
        "error": FAILURE_REASON,
        "trigger_outcome": "crashed",
        "force_trip": True,
    }


def test_a_failed_failure_wake_archives_the_card_it_filed(
    hermes_root: Path, tmp_path: Path
) -> None:
    shell = _shell_for(hermes_root, tmp_path, FAILURE_PROMPT, FAKE_WAKE_FAILS="1")

    with pytest.raises(card_wake.ReplayBroken, match="failure wake: .*notifier exploded"):
        _plant(shell, FAILURE_PROMPT)

    [card] = _board(tmp_path)["tasks"].values()
    assert card["status"] == "archived"


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
    wake = AgentResult(output="a", trajectory=[], errors=["first"])
    answer = AgentResult(output="b", trajectory=[], errors=["second"])

    assert card_wake.merge(_PLANTED, wake, answer).errors == ["first", "second"]


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
    assert tagged.metadata["failure_wake"] == {
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
        "args": {"card": "t_1"},
        "result": {"status": "ready", "comments": [{"author": "default", "body": "seeded-b"}]},
        "status": "harness",
    }
