"""inspect-repository-unmanaged-and-context-read's route check, against the verifier.

The case guards the snapshot route: a worker reads both repositories through
`inspect_repository.py clone`, and no command uses a broker workspace handle.
These run the case's own check through `WorkerCommandsVerifier` on worker log
lines as hermes renders them: the snapshot route passes, with or without a
direct download beside it, a leading `cd`, a chain the clone starts, a quoted
path and a continued line do not hide it, a grep through its pattern
argument does not count, and the red's workspace route or verb fails, whatever
client, interpreter, wrapper, pipe, heredoc or assignment carries it.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "inspect-repository-unmanaged-and-context-read"
    / "task.yaml"
)
CHECK = "the-snapshot-route-and-no-workspace-handle"
SCRIPT = "/opt/data/profiles/platform/skills/inspect-repository/scripts/inspect_repository.py"
CLONE_PUBLIC = f"python3 {SCRIPT} clone --repo gke-labs/kube-agents --ref 0.13.0 --prefix tags.env --into ./kube-agents"
CLONE_CONTEXT = f"python3 {SCRIPT} clone --repo kubernetes-sigs/kind --ref v0.24.0 --into ./kind"


def _passes(*rows: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    transcript.set("", [], worker_commands=[{"task": "t_1", "command": row} for row in rows])
    try:
        result = WorkerCommandsVerifier(**check["check"]).verify(5.0)
    finally:
        transcript.clear()
    assert result.status in ("pass", "fail"), result.reason
    return result.success


def test_the_snapshot_route_passes():
    assert _passes(CLONE_PUBLIC, CLONE_CONTEXT, "cat ./kind/pkg/apis/config/defaults/image.go")


def test_a_direct_download_beside_the_snapshot_route_still_passes():
    # The first-move download exists on main; the follow-up issue owns it.
    assert _passes("curl -s https://raw.githubusercontent.com/gke-labs/kube-agents/0.13.0/tags.env", CLONE_PUBLIC)


def test_a_clone_after_cd_or_at_the_head_of_a_chain_is_seen():
    # hermes skips a leading `cd` and keeps a chain's first command.
    assert _passes(CLONE_PUBLIC + " + 1 command(s)")
    assert _passes(CLONE_CONTEXT)


def test_the_red_s_workspace_route_fails():
    opened = f"H=$(python3 {SCRIPT} open --repo gke-labs/kube-agents --depth 1)"
    assert not _passes(opened, "curl -s https://raw.githubusercontent.com/gke-labs/kube-agents/0.13.0/tags.env")
    assert not _passes(opened, CLONE_PUBLIC)


RED_RUN = Path(__file__).resolve().parent / "fixtures" / "inspect_repository_red_20261103.json"


def _grade_calls(trajectory: list[dict]):
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    # No log line for the card, so the verifier reads its tagged calls as typed.
    transcript.set("", trajectory, worker_commands=[])
    try:
        return WorkerCommandsVerifier(**check["check"]).verify(5.0)
    finally:
        transcript.clear()


def test_the_recorded_red_run_fails_on_its_open_call():
    # The worker's calls from the 2026-11-03 red run (main before the snapshot
    # move). That main cannot be installed on cw-host after #4688, so this
    # check grades the recorded calls. The run fails on its `open` call, and
    # the same calls without it pass.
    calls = json.loads(RED_RUN.read_text())["trajectory"]
    result = _grade_calls(calls)
    assert result.status == "fail", result.reason
    assert "forbidden run(s) made by worker commands: inspect_repository.py ran as" in result.reason
    assert f"{SCRIPT} open" in result.reason
    without_open = [c for c in calls if f"{SCRIPT} open " not in c["args"].get("command", "")]
    assert len(without_open) == len(calls) - 1
    assert _grade_calls(without_open).status == "pass"


def test_no_clone_at_all_fails():
    assert not _passes("curl -s https://raw.githubusercontent.com/gke-labs/kube-agents/0.13.0/tags.env")


def test_a_quoted_path_or_a_continued_line_is_still_the_clone_run():
    # A quoted path is one word whichever quote it has. hermes joins a
    # backslash-newline with a space, so the row carries `\ clone`.
    assert _passes(f"python3 '{SCRIPT}' clone --repo gke-labs/kube-agents --ref 0.13.0 --prefix tags.env")
    assert _passes(f'python3 "{SCRIPT}" clone --repo gke-labs/kube-agents --ref 0.13.0 --prefix tags.env')
    assert _passes(f"python3 {SCRIPT} \\ clone --repo kubernetes-sigs/kind --ref v0.24.0 --into ./kind")
    assert not _passes(f"python3 {SCRIPT} \\ open --repo gke-labs/kube-agents --depth 1", CLONE_PUBLIC)


def test_a_mention_is_no_run():
    # A grep names the route or a verb as its pattern argument, and a cat
    # reads a file that names them; none of them runs it.
    assert _passes('grep "inspect_repository.py open" SKILL.md', CLONE_PUBLIC)
    assert _passes('grep -rn "/v1/workspace" /opt/data/profiles/platform/skills', CLONE_PUBLIC)
    assert _passes("cat /opt/data/profiles/platform/skills/inspect-repository/SKILL.md", CLONE_PUBLIC)
    assert not _passes(f'echo "note: run python3 {SCRIPT} clone --repo gke-labs/kube-agents"')


def test_a_grep_through_its_pattern_argument_is_exempt():
    # The exemption removes the grep, its options and its pattern argument;
    # the file operands that stay name no route.
    for row in (
        "grep -rn /v1/workspace /opt/data/profiles",
        "grep -rn /v1/workspace /opt/data/profiles 2>/dev/null",
        "grep -rn /v1/workspace /opt/data/profiles > /dev/null",
        "grep -rn 'URL=http://127.0.0.1:8080/v1/workspace/open' SKILL.md",
        "  egrep -rn -e /v1/workspace -e 'inspect_repository.py list' /opt/data/profiles",
        "fgrep -A 2 'inspect_repository.py \\ fetch' SKILL.md",
        # The quoted pattern is removed before the operator rule reads the
        # row, so its `<` is not an operator.
        "grep -n 'python3 - <<EOF /v1/workspace' SKILL.md",
    ):
        assert _passes(row, CLONE_PUBLIC), row


def test_a_call_beside_a_read_on_the_row_fails():
    # A write, a pipe, a substitution, a chain or a second line puts a call
    # on the row. hermes joins a second line with a space, and the grep
    # exemption leaves the joined call to be graded. A collapsed chain hides
    # what followed, so the exemption refuses it.
    url = "http://127.0.0.1:8080/v1/workspace/open"
    for row in (
        'echo "Opening workspace" curl -s -X POST "$BROKER/v1/workspace/open"',
        "grep -rn kind SKILL.md curl -s $BROKER/v1/workspace/open",
        "grep -rn /v1/workspace SKILL.md curl -s $BROKER/v1/workspace/open",
        f"grep -rn kind SKILL.md python3 {SCRIPT} open --repo gke-labs/kube-agents",
        f"grep -rn open SKILL.md 2>/dev/null; python3 {SCRIPT} open --repo gke-labs/kube-agents",
        "grep -rn /v1/workspace . 2>/dev/null | sh",
        # A pipe with no redirection: the `|` rule alone refuses it.
        "grep -ohE 'curl -s [^ ]*/v1/workspace/open' SKILL.md | sh",
        "grep -oh 'http[^ ]*/v1/workspace/open' SKILL.md | xargs curl -s -X POST",
        "grep -rn /v1/workspace . > /tmp/call.sh",
        f"cat > /tmp/call.py <<'EOF' import urllib.request urllib.request.urlopen('{url}') EOF",
        f"cat > /tmp/call.py <<'EOF'\nimport urllib.request\nurllib.request.urlopen('{url}')\nEOF",
        'echo \'{"repo":"kind"}\' | curl -s -X POST -d @- "$BROKER/v1/workspace/open"',
        'echo "$(curl -s $BROKER/v1/workspace/list)"',
        'echo "curl -s $BROKER/v1/workspace/open" | sh',
        "echo `curl -s $BROKER/v1/workspace/list`",
        "grep -rn kind SKILL.md\ncurl -s $BROKER/v1/workspace/open",
        "grep -rn kind SKILL.md; curl -s $BROKER/v1/workspace/open",
        "grep -rn kind SKILL.md && curl -s $BROKER/v1/workspace/open",
        "printf 'urlopen(\"http://x/v1/workspace/open\")' > /tmp/call.py",
        'cd "$(curl -s $BROKER/v1/workspace/open)" && ls',
        "grep -rn /v1/workspace . + 1 command",
        "sed -i 's#X#/v1/workspace/open#' /tmp/call.py",
        "find . -exec curl -s http://x/v1/workspace/open {} +",
        # An operator glued inside the grep's pattern word starts a second command.
        "grep x|{curl,http://x/v1/workspace/open}",
        "grep x;curl${IFS}http://x/v1/workspace/open SKILL.md",
        "grep -e;curl${IFS}http://x/v1/workspace/open SKILL.md",
        "grep x&curl${IFS}http://x/v1/workspace/open",
        "headless-client http://127.0.0.1:8080/v1/workspace/open",
        # A `cd` directory may not carry a call: an escaped quote that bash
        # keeps open, a backquote, or a substitution in a bare word.
        'cd "x\\" && cd "; curl http://x/v1/workspace/open; " && grep foo "',
        'cd "x\\"; cd "; curl http://x/v1/workspace/open; "; ls "',
        'cd "`curl -s http://x/v1/workspace/open`" && ls',
        "cd $(curl${IFS}http://x/v1/workspace/open) && ls",
        "cd `curl${IFS}http://x/v1/workspace/open` && ls",
    ):
        assert not _passes(row, CLONE_PUBLIC), row


def test_a_substitution_in_a_grep_pattern_is_not_exempt():
    # A bare word or a double-quoted pattern stops at `$(` or a backquote,
    # and a bare word also at <, >, ( or ), so the exemption leaves the
    # substitution on the row to be graded.
    url = "http://127.0.0.1:8080/v1/workspace/open"
    for row in (
        f"grep x$(curl${{IFS}}{url}) SKILL.md",
        f"grep x`curl${{IFS}}{url}` SKILL.md",
        f"grep -e$(curl${{IFS}}{url}) SKILL.md",
        f"grep -e x$(curl${{IFS}}{url}) SKILL.md",
        f"grep -f <(curl${{IFS}}{url}) SKILL.md",
        'grep "$(curl -s -X POST http://127.0.0.1:8080/v1/workspace/open)" SKILL.md',
        'grep "`curl -s -X POST http://127.0.0.1:8080/v1/workspace/open`" SKILL.md',
        'grep -e "$(curl -s -X POST http://127.0.0.1:8080/v1/workspace/open)" SKILL.md',
    ):
        assert not _passes(row, CLONE_PUBLIC), row


def test_a_read_that_is_not_one_grep_counts_as_a_call():
    # A spurious red that the case header records: only one grep through its
    # pattern argument is exempt, so any other read that names the route or a
    # verb counts as a call.
    for row in (
        "echo URL=http://127.0.0.1:8080/v1/workspace/open",
        f'echo "next: python3 {SCRIPT} open --repo gke-labs/kube-agents"',
        "printf '%s\\n' /v1/workspace/open",
        "cat /tmp/notes/v1/workspace.md",
        "head -n 5 /tmp/v1/workspace/open.log",
        "rg -n /v1/workspace /opt/data/profiles",
        "if grep -q /v1/workspace SKILL.md; then :; fi",
        "timeout 5 grep -rn /v1/workspace .",
        "/usr/bin/grep -rn /v1/workspace .",
        "sed -n 's#/v1/workspace#X#p' /opt/data/scripts/credential_proxy_client.py",
        "grep -rn /v1/workspace . + 1 command",
    ):
        assert not _passes(row, CLONE_PUBLIC), row


def test_a_cd_step_before_a_grep_on_a_typed_card_is_not_exempt():
    # A worker-filed card's call is read as typed, with its `cd` step, and
    # the exemption reads only a grep at the start of the row. This is a
    # spurious red that the case header records.
    for row in (
        "cd /opt/data/profiles && grep -rn /v1/workspace .",
        "cd /opt/data/profiles; grep -rn /v1/workspace .",
        "cd /opt/data/profiles\ngrep -rn /v1/workspace .",
    ):
        assert not _passes(row, CLONE_PUBLIC), row


def test_a_grep_with_a_pipe_a_separate_option_value_or_a_merged_trailer_is_not_exempt():
    # A spurious red that the case header records: a pipe is refused, an
    # option's separate value reads as the pattern so the route stays as a
    # file operand, and only a `2>/dev/null` or `> /dev/null` trailer is kept.
    for row in (
        "grep -rn /v1/workspace /opt/data/profiles | head -20",
        "grep -rn --include '*.md' /v1/workspace /opt/data/profiles",
        "grep --context 3 /v1/workspace SKILL.md",
        "grep -rn /v1/workspace SKILL.md 2>&1",
        "grep -rn /v1/workspace SKILL.md &>/dev/null",
    ):
        assert not _passes(row, CLONE_PUBLIC), row


def test_the_workspace_route_through_curl_wget_or_python_fails():
    assert not _passes('curl -s -X POST "$BROKER/v1/workspace/open" -d \'{"repo":"kind"}\'', CLONE_PUBLIC)
    assert not _passes("wget -qO- http://127.0.0.1:8080/v1/workspace/list", CLONE_PUBLIC)
    assert not _passes(
        "python3 -c \" import urllib.request urllib.request.urlopen('http://127.0.0.1:8080/v1/workspace/open') \"",
        CLONE_PUBLIC,
    )
    assert not _passes(
        "python -c \" import urllib.request urllib.request.urlopen('http://127.0.0.1:8080/v1/workspace/open') \"",
        CLONE_PUBLIC,
    )
    assert not _passes(
        "python3.14 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/v1/workspace/open')\"",
        CLONE_PUBLIC,
    )
    # The base URL in a variable is not resolved, but the word still carries the route.
    assert not _passes("H=$(curl -s $BROKER/v1/workspace/open) + 1 command", CLONE_PUBLIC)


def test_a_refused_run_still_counts():
    # A row carries no result: a clone the script refused still meets the
    # required run, and an open argparse refused still fails the check. This
    # is the known limit #4363 owns.
    assert _passes(f"python3 {SCRIPT} clone --repo gke-labs/kube-agents --into /opt/data/scratch/not-empty")
    assert not _passes(f"python3 {SCRIPT} open --rpo gke-labs/kube-agents", CLONE_PUBLIC)


def test_the_workspace_route_through_python_standard_input_fails():
    # hermes joins the heredoc into one row, and the row names the route.
    assert not _passes(
        "python3 - <<'EOF' import urllib.request as u url = 'http://127.0.0.1:8080/v1/workspace/open' EOF + 1 command",
        CLONE_PUBLIC,
    )
    assert not _passes(
        "timeout 30 python3 - <<'EOF' url = 'http://127.0.0.1:8080/v1/workspace/open' EOF",
        CLONE_PUBLIC,
    )
    assert not _passes(
        "python3 - <<'EOF' import urllib.request urllib.request.urlopen('http://127.0.0.1:8080/v1/workspace/open') EOF",
        CLONE_PUBLIC,
    )
    # Python reads standard input with no `-` too, and a wrapper may stand in front.
    assert not _passes(
        "python3 <<'EOF' import urllib.request urllib.request.urlopen('http://127.0.0.1:8080/v1/workspace/open') EOF",
        CLONE_PUBLIC,
    )
    assert not _passes(
        "timeout 30 python3 - <<'EOF' import urllib.request urllib.request.urlopen('http://127.0.0.1:8080/v1/workspace/open') EOF",
        CLONE_PUBLIC,
    )
    # A heredoc a worker-filed card keeps over several lines.
    assert not _passes(
        "python3 <<'EOF'\nimport urllib.request\nurllib.request.urlopen('http://127.0.0.1:8080/v1/workspace/open')\nEOF",
        CLONE_PUBLIC,
    )


def test_an_assignment_that_holds_the_route_fails():
    # hermes keeps the assignment as the chain's first segment.
    assert not _passes("URL=http://127.0.0.1:8080/v1/workspace/open + 1 command", CLONE_PUBLIC)


def test_a_route_that_no_row_names_is_not_seen():
    # A known limit that the case header records: a script file that holds the
    # route, or a variable that an earlier row set, is a row with no route in it.
    assert _passes("python3 /tmp/call_broker.py", CLONE_PUBLIC)
    assert _passes('curl -s -X POST "$WORKSPACE_URL/open"', CLONE_PUBLIC)
    # The rendering keeps a chain's first command, so a call behind it is not on the row.
    assert _passes("cat SKILL.md + 1 command(s)", CLONE_PUBLIC)
    # A row that names the route as an argument is not a mention, whatever it runs.
    assert not _passes("python3 /tmp/x.py --url http://127.0.0.1:8080/v1/workspace/open", CLONE_PUBLIC)


def test_a_route_written_another_way_is_not_seen():
    # A known limit that the case header records: the route pattern reads
    # the path as written.
    for row in (
        "curl -s http://x/v1/./workspace/open",
        "curl -s http://x/v1//workspace/open",
        "curl -s http://x/v1/works''pace/open",
        "curl -s http://x/v1/%77orkspace/open",
        # `${IFS}` in place of a space: no run splits on it, and no pattern reads it as one.
        f"python3 {SCRIPT}${{IFS}}open --repo gke-labs/kube-agents",
        f"grep x$(python3${{IFS}}{SCRIPT}${{IFS}}open) SKILL.md",
    ):
        assert _passes(row, CLONE_PUBLIC), row


def test_a_verb_inside_a_shell_heredoc_fails():
    # hermes joins the heredoc into one row, an argv of `bash` and no run of
    # the script, so the verb text pattern grades it.
    for verb in ("open", "list", "grep", "fetch", "close"):
        row = f"bash <<'EOF' python3 {SCRIPT} {verb} --repo gke-labs/kube-agents EOF"
        assert not _passes(row, CLONE_PUBLIC), row
    for row in (
        f"sh <<'EOF' python3 '{SCRIPT}' open --repo gke-labs/kube-agents EOF",
        f'bash <<EOF python3 "{SCRIPT}" open --repo gke-labs/kube-agents EOF',
        f"bash <<'EOF' python3 {SCRIPT} \\ open --repo gke-labs/kube-agents EOF",
        f'bash <<\'EOF\' python3 {SCRIPT} "open" --repo gke-labs/kube-agents EOF',
        f"bash <<'EOF' python3 {SCRIPT} 'open' --repo gke-labs/kube-agents EOF",
    ):
        assert not _passes(row, CLONE_PUBLIC), row


def test_a_clone_inside_a_shell_heredoc_is_not_credited():
    # A spurious red that the case header records, when the card's tagged
    # call did not reach the trajectory: the row is an argv of `bash`, so it
    # does not meet the required run. Beside a typed clone, the heredoc row
    # passes.
    row = f"bash <<'EOF' python3 {SCRIPT} clone --repo gke-labs/kube-agents --prefix tags.env EOF"
    assert not _passes(row)
    assert _passes(row, CLONE_PUBLIC)


def test_a_heredoc_note_typed_on_a_card_counts_as_a_run():
    # A known limit that the case header records: a worker-filed card's call
    # is read as typed, and each line of a heredoc body is a command. A note
    # that quotes a verb run fails, and one that quotes a clone run passes.
    assert not _passes(f"cat > /tmp/notes.md <<'EOF'\npython3 {SCRIPT} open --repo x\nEOF", CLONE_PUBLIC)
    assert _passes(f"cat > /tmp/notes.md <<'EOF'\npython3 {SCRIPT} clone --repo x\nEOF")


def test_the_verb_words_are_whole_words():
    # The anchors: `openssl` is no `open`, and `./clone` is no `clone`.
    assert _passes(f"python3 {SCRIPT} clone --repo openssl/openssl --prefix crypto --into ./src")
    assert not _passes(f"python3 {SCRIPT} --into ./clone")


def test_each_workspace_verb_fails():
    for verb in ("list", "grep", "fetch", "close"):
        assert not _passes(f"python3 {SCRIPT} {verb} --repo gke-labs/kube-agents", CLONE_PUBLIC), verb


def test_a_verb_as_an_option_value_counts_as_the_verb():
    # A run cannot name a word position, so `--into list` reads as the verb
    # `list`. This is a known limit that the case header records.
    assert not _passes(f"python3 {SCRIPT} clone --repo gke-labs/kube-agents --into list")


def test_a_help_request_on_a_verb_counts_as_the_verb():
    # A spurious red that the case header records: `open -h` only prints the
    # help, but the verb text pattern reads the script with the verb.
    assert not _passes(f"python3 {SCRIPT} open -h", CLONE_PUBLIC)


def test_any_interpreter_or_flag_form_that_names_the_route_fails():
    # The text pattern does not depend on the command word or its flags.
    url = "http://127.0.0.1:8080/v1/workspace/open"
    for row in (
        f"python3.11 -c \"import urllib.request; urllib.request.urlopen('{url}')\"",
        f"python3 \\ -c \"import urllib.request; urllib.request.urlopen('{url}')\"",
        f"python3 -Ic \"import urllib.request; urllib.request.urlopen('{url}')\"",
        f"node -e \"fetch('{url}')\"",
        f"http POST {url}",
    ):
        assert not _passes(row, CLONE_PUBLIC), row
