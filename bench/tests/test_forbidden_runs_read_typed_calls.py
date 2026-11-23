"""``forbidden_runs`` also read a logged card's ``terminal`` calls as typed (#6106).

A card log row is the call on one line: a newline becomes a space, and a
chain arrives as its first command and a count. So a forbidden run on a later
line of a call, or behind ``;``, ``&&`` or ``||``, is on no row. The required
runs read a logged card's typed calls since #4620; the forbidden runs now do
too, with one exception: a typed call that holds a heredoc stays on its row,
because the argv reader splits a heredoc body into one argv for each line,
and a line written into a file is not a run.

``refused_result_pattern`` names the result of a call that was refused: the
forbidden runs do not read such a typed call of one command, or one row equal
to it. A call of several commands has one result for all of them, so it is read.

The commands are written for this test; none is copied from a record.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

CARD = "t_1"
READ = "gcloud monitoring policies list --project=p"
WRITE = "gcloud monitoring policies delete projects/p/alertPolicies/1 --quiet"
REFUSAL = "Agents hold read-only access. policy rule: gcloud.monitoring.write"
# One level more than the shell reader reads (`_MAX_COMMAND_LINE_NESTING` is 16).
PAST_THE_LIMIT = "eval " * 17 + "echo ok"
CHECK = {
    "type": "worker_commands",
    "forbidden_runs": [{"script": "gcloud", "word_patterns": ["^monitoring$", "^policies$", "^(create|update|delete)$"]}],
}


def _call(command: str, result: str = "", tool: str = "terminal", card: str = CARD) -> dict:
    return {
        "name": tool,
        "args": {"command" if tool == "terminal" else "code": command},
        "result": result,
        "status": "error" if result == REFUSAL else "completed",
        "agent": "platform",
        "task": card,
        "session": "s_1",
        "at": 100.0,
    }


def _row(command: str, card: str = CARD) -> dict:
    """A card log row as hermes renders a call: on one line, a chain as its first command and a count."""
    line = " ".join(command.split())
    for operator in (" && ", " || ", "; "):
        if operator in line:
            line = line.split(operator)[0] + " + 1 command"
            break
    return {"task": card, "command": line}


def _result(calls: list[dict], rows: list[dict] | None = None, **options):
    """The check on a logged card: each terminal call has its row, unless ``rows`` names them."""
    transcript.set("ok", calls, worker_commands=[_row(c["args"]["command"]) for c in calls if c["name"] == "terminal"] if rows is None else rows)
    try:
        return WorkerCommandsVerifier(**CHECK, **options).verify(5.0)
    finally:
        transcript.clear()


def _status(calls: list[dict], rows: list[dict] | None = None, **options) -> str:
    return _result(calls, rows, **options).status


@pytest.mark.parametrize(
    "command",
    [
        f"ls -la /opt/data\n{WRITE}",
        f"cd /opt/data\nexport P=p\n{WRITE}",
        f"{READ}; {WRITE}",
        f"{READ} && {WRITE}",
        f"{READ} || {WRITE}",
        f"for p in a b; do\n  {WRITE}\ndone",
    ],
)
def test_a_run_that_the_row_does_not_show_is_read_in_the_typed_call(command):
    result = _result([_call(command)])
    assert result.status == "fail" and "gcloud ran as" in result.reason, result.reason


def test_a_run_on_the_row_is_counted_one_time():
    result = _result([_call(WRITE)])
    assert result.status == "fail" and result.reason.count("gcloud ran as") == 1, result.reason


@pytest.mark.parametrize(
    "command",
    [
        f"{READ}\necho done",
        f'{READ}\ngrep -n "{WRITE}" SKILL.md',
        f"{READ}\npython3 -c 'print(\"{WRITE}\")'",
    ],
)
def test_a_typed_call_with_no_forbidden_run_passes(command):
    assert _status([_call(command)]) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        f"cat > /tmp/later.sh <<'EOF'\n{WRITE}\nEOF",
        f"cat > /opt/data/scratch/notes.md <<EOF\nTo fix by hand:\n{WRITE}\nEOF",
        f"cat <<-EOF > notes.md\n\t{WRITE}\n\tEOF",
    ],
)
def test_a_line_of_a_heredoc_body_is_not_a_run(command):
    # The recorded reason for the row read (#4620): the typed text splits a heredoc into one
    # argv for each line. A call that holds a heredoc stays on its row.
    assert _status([_call(command)]) == "pass"


def test_known_gap_a_run_on_a_later_line_of_a_call_with_a_heredoc_is_not_read():
    command = f"cat > notes.md <<'EOF'\nsummary\nEOF\n{WRITE}"
    assert _status([_call(command)]) == "pass"


def test_a_here_string_is_not_a_heredoc():
    assert _status([_call(f"grep -c policies <<< 'policies'\n{WRITE}")]) == "fail"


def test_the_program_text_of_a_logged_card_is_not_read():
    program = f"import os\nnote = 'run this'\n{WRITE}"
    assert _status([_call(READ), _call(program, tool="execute_code")]) == "pass"


def test_a_card_with_no_row_is_read_from_its_calls_as_before():
    rows = [_row(READ, card="t_0")]
    assert _status([_call(READ, card="t_0"), _call(f"{READ}\n{WRITE}", card="t_2")], rows) == "fail"
    assert _status([_call(READ, card="t_0"), _call(f"cat > x <<'EOF'\n{WRITE}\nEOF", card="t_2")], rows) == "fail"


def test_rows_that_name_no_card_are_read_as_before():
    rows = [{"command": " ".join(READ.split())}]
    assert _status([_call(f"{READ}\n{WRITE}")], rows) == "fail"


def test_the_forbidden_patterns_stay_on_the_rows():
    check = {"type": "worker_commands", "forbidden_patterns": [r"policies\s+delete"]}
    transcript.set("ok", [_call(f"{READ}\n{WRITE}")], worker_commands=[{"task": CARD, "command": READ + " + 1 command"}])
    try:
        assert WorkerCommandsVerifier(**check).verify(5.0).status == "pass"
    finally:
        transcript.clear()


def test_a_refused_call_is_read_without_the_option():
    assert _status([_call(f"{READ}\n{WRITE}", REFUSAL)]) == "fail"
    assert _status([_call(WRITE, REFUSAL)]) == "fail"


def test_the_option_skips_a_refused_call_of_one_command_and_one_row_equal_to_it():
    option = {"refused_result_pattern": "Agents hold read-only access"}
    assert _status([_call(WRITE, REFUSAL)], **option) == "pass"
    # A call that was not refused is read, also beside a refused call with the same text.
    assert _status([_call(WRITE, "deleted")], **option) == "fail"
    assert _status([_call(WRITE, REFUSAL), _call(WRITE, "deleted")], **option) == "fail"
    # One refused call skips one row: a second equal row, whose call did not reach the
    # trajectory, is read.
    assert _status([_call(WRITE, REFUSAL)], rows=[_row(WRITE), _row(WRITE)], **option) == "fail"


@pytest.mark.parametrize(
    "command, rows",
    [
        # the first line was refused and the second ran: one result for the call
        (f"kubectl delete pod x\n{WRITE}", None),
        (f"kubectl delete pod x; {WRITE}", None),
        (f"{WRITE} && echo done", None),
        # a result that quotes the refusal words from a file, beside a write that ran
        (f"cat policy.md\n{WRITE}", None),
    ],
)
def test_the_option_does_not_skip_a_call_of_several_commands(command, rows):
    assert _status([_call(command, REFUSAL)], rows, refused_result_pattern="Agents hold read-only access") == "fail"


def test_the_option_skips_no_row_that_names_no_card():
    rows = [{"command": WRITE}]
    assert _status([_call(WRITE, REFUSAL)], rows, refused_result_pattern="Agents hold read-only access") == "fail"


def test_the_option_skips_a_refused_call_of_a_card_with_no_row():
    option = {"refused_result_pattern": "Agents hold read-only access"}
    rows = [_row(READ, card="t_0")]
    calls = [_call(READ, card="t_0"), _call(WRITE, REFUSAL, card="t_2")]
    assert _status(calls, rows) == "fail"
    assert _status(calls, rows, **option) == "pass"


def test_the_option_skips_a_refused_call_under_a_window():
    check = {**CHECK, "after_result_pattern": "(?!)"}
    transcript.set("ok", [_call(READ), _call(WRITE, REFUSAL)], worker_commands=[])
    try:
        assert WorkerCommandsVerifier(**check).verify(5.0).status == "fail"
        assert WorkerCommandsVerifier(**check, refused_result_pattern="read-only access").verify(5.0).status == "pass"
    finally:
        transcript.clear()


def test_a_typed_call_past_the_readers_limit_is_an_error_not_a_pass():
    # The shell reader gives no argvs for a line past its limit, so a check that reads argvs
    # cannot be evaluated on it. The row shows the first command of the chain only; the typed
    # call that the forbidden runs now read holds the line past the limit.
    command = f"{READ}; {PAST_THE_LIMIT}"
    assert _status([_call(command)], rows=[_row(command)]) == "error"
    # A forbidden run that was read is conclusive whatever else was not.
    assert _status([_call(f"{READ}\n{WRITE}"), _call(PAST_THE_LIMIT)]) == "fail"
    # A refused call past the limit is not known to be one command, so the option does not skip it.
    assert _status([_call(PAST_THE_LIMIT, REFUSAL)], refused_result_pattern="read-only access") == "error"


def test_the_option_needs_forbidden_runs_and_a_pattern_that_compiles():
    with pytest.raises(ValidationError):
        WorkerCommandsVerifier(type="worker_commands", required_patterns=["x"], refused_result_pattern="refused")
    with pytest.raises(ValidationError):
        WorkerCommandsVerifier(**CHECK, refused_result_pattern="")
    with pytest.raises(re.error):
        WorkerCommandsVerifier(**CHECK, refused_result_pattern="(")
