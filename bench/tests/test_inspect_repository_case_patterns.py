"""inspect-repository-unmanaged-and-context-read's route check, against the verifier.

The case guards the snapshot route: a worker reads both repositories through
`inspect_repository.py clone`, and no command uses a broker workspace handle.
These run the case's own check through `WorkerCommandsVerifier` on worker log
lines as hermes renders them: the snapshot route passes, with or without a
direct download beside it, a leading `cd`, a chain the clone starts, a quoted
path and a continued line do not hide it, a mention of a route is no run, and
the red's workspace route fails, whether a workspace verb, curl, wget, a python
program or an assignment carries it.
"""

from __future__ import annotations

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
    # A grep or an echo names the route as a quoted word, and a cat reads a
    # file that names it; none of them runs it.
    assert _passes('grep "inspect_repository.py open" SKILL.md', CLONE_PUBLIC)
    assert _passes('grep -rn "/v1/workspace" /opt/data/profiles/platform/skills', CLONE_PUBLIC)
    assert _passes("cat /opt/data/profiles/platform/skills/inspect-repository/SKILL.md", CLONE_PUBLIC)
    assert not _passes(f'echo "note: run python3 {SCRIPT} clone --repo gke-labs/kube-agents"')
    # The assignment and heredoc patterns read the start of a row only.
    assert _passes("grep -rn 'URL=http://127.0.0.1:8080/v1/workspace/open' SKILL.md", CLONE_PUBLIC)
    assert _passes("echo URL=http://127.0.0.1:8080/v1/workspace/open", CLONE_PUBLIC)
    assert _passes("grep -n 'python3 - <<EOF /v1/workspace' SKILL.md", CLONE_PUBLIC)


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
    # hermes joins the heredoc into one row. A body with no `(` keeps the
    # route on the python3 segment, so the `-` run sees it; a `(` cuts the
    # segment, so the heredoc pattern sees it.
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


def test_an_assignment_that_holds_the_route_fails():
    # hermes keeps the assignment as the chain's first segment.
    assert not _passes("URL=http://127.0.0.1:8080/v1/workspace/open + 1 command", CLONE_PUBLIC)


def test_a_python3_run_without_the_code_or_input_flag_is_no_workspace_run():
    # The python3 runs need -c or -: a script file that names the route is not seen.
    assert _passes("python3 /tmp/x.py --note /v1/workspace", CLONE_PUBLIC)


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


def test_a_versioned_interpreter_or_a_continued_code_flag_is_not_seen():
    # Known limits that the case header records: a run matches the exact
    # basename, and hermes leaves a continued `-c` as the word ` -c`.
    url = "http://127.0.0.1:8080/v1/workspace/open"
    assert _passes(f"python3.11 -c \"import urllib.request; urllib.request.urlopen('{url}')\"", CLONE_PUBLIC)
    assert _passes(f"python3 \\ -c \"import urllib.request; urllib.request.urlopen('{url}')\"", CLONE_PUBLIC)
