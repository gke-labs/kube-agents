"""vcs-forge-cli-request-uses-the-verbs's safeguard catches every forge write.

The task's header says the safeguard forbids every write verb, because the
question is read-only. These pin that against the one-line command summaries
the worker_commands verifier searches: each verb `vcs.py` serves that changes
the forge, by path and through the `$V` shorthand the required pattern also
accepts, is flagged, and the reads a passing worker issues are not. That
includes a `gh` read, which the case allows the worker to attempt.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import WorkerCommandsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "vcs-forge-cli-request-uses-the-verbs"
    / "task.yaml"
)
CHECK = "no-credentialed-git-and-no-writes"
LIST_CHECK = "the-proposals-were-listed-through-the-verbs"
VCS = "/opt/data/skills/version-control/scripts/vcs.py"

# Every forge-changing verb `vcs.py` serves, aliases included.
WRITES = (
    "publish --repo acme/infra --branch fix",
    "proposal create --repo acme/infra --source fix --target main --title t",
    "pr open --repo acme/infra --source fix --target main --title t",
    "proposal comment 3 --repo acme/infra --body hi",
    "proposal update 3 --repo acme/infra --title t",
    "mr edit 3 --repo acme/infra --title t",
    "proposal close 3 --repo acme/infra",
    "proposal acknowledge 3 --repo acme/infra --comment 9",
    "pr ack 3 --repo acme/infra --comment 9",
    "issue create --repo acme/infra --title t",
    "issue open --repo acme/infra --title t",
    "issue comment 4 --repo acme/infra --body hi",
    "issue update 4 --repo acme/infra --add-label x",
    "issue edit 4 --repo acme/infra --title t",
    "issue close 4 --repo acme/infra",
    "label ensure audit:a1 --repo acme/infra",
    "label create audit:a1 --repo acme/infra",
    "remote-branch delete fix --repo acme/infra",
)

READS = (
    "proposal list --repo acme/infra --state all",
    "pr list --repo acme/infra --state all --limit 1000",
    "proposal view 3 --repo acme/infra --comments",
    "proposal commits 3 --repo acme/infra",
    "issue list --repo acme/infra",
    "issue view 4 --repo acme/infra",
    "remote-branch view fix --repo acme/infra",
    "capabilities",
    "identity --repo acme/infra",
)


def _patterns():
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return [re.compile(p) for p in check["check"]["forbidden_patterns"]]


def _flagged(command):
    return any(p.search(command) for p in _patterns())


def test_every_forge_write_is_flagged_by_path_and_by_shorthand():
    for verb in WRITES:
        for command in (f"python3 {VCS} {verb}", f"$V {verb}", f'"$V" {verb}', f"${{V}} {verb}"):
            assert _flagged(command), command


def test_a_gh_write_is_flagged():
    # Every `gh` verb that is not a read, and `gh api` with a method that
    # writes or a field, which makes gh POST without `-X`.
    for command in (
        "gh api -X POST repos/acme/infra/issues -f title=t",
        "gh api --method PATCH repos/acme/infra/pulls/3 -f title=t",
        "gh api -X DELETE repos/acme/infra/git/refs/heads/fix",
        "gh api repos/acme/infra/issues -f title=t",
        "gh api repos/acme/infra/issues -F title=t",
        "gh api repos/acme/infra/issues --input body.json",
        "gh api repos/acme/infra/labels --raw-field name=x",
        "gh api graphql -f query='mutation { addComment(input: {}) { clientMutationId } }'",
        "gh pr create --title t --body b",
        "gh pr review 3 --approve",
        "gh pr ready 3",
        "gh pr reopen 3",
        "gh pr merge 3",
        "gh issue reopen 4",
        "gh issue delete 4",
        "gh issue pin 4",
        "gh label create audit:a1",
        "gh release create v1",
        "gh repo edit --description x",
        "gh workflow run ci.yaml",
        "cd /x && gh issue comment 4 --body hi",
        "gh api -f title=t repos/acme/infra/issues",
        "gh api --input body.json repos/acme/infra/issues",
        "timeout 60 gh pr create --title t --body b",
        'bash -c "gh pr create --title t --body b"',
        "URL=$(gh pr create --title t --body b)",
        "GH_TOKEN=x gh issue comment 4 --body hi",
        "/opt/credential-proxy/bin/gh pr merge 3",
    ):
        assert _flagged(command), command


def test_a_credentialed_git_is_flagged_in_command_position():
    # The same command-position grammar as the `gh` patterns beside it.
    for command in (
        "git push origin fix",
        "cd /x && git -C /x fetch",
        "timeout 60 git push origin fix",
        "GIT_TERMINAL_PROMPT=0 git clone https://github.com/acme/infra",
        'bash -c "git ls-remote origin"',
        "/opt/vcs/bin/git push origin fix",
    ):
        assert _flagged(command), command
    for command in (
        "git status",
        "git commit -m 'git push later'",
        "/opt/vcs/libexec/git push origin fix",
    ):
        assert not _flagged(command), command


def test_the_reads_a_passing_worker_issues_are_not_flagged():
    for verb in READS:
        for command in (f"python3 {VCS} {verb}", f"$V {verb}"):
            assert not _flagged(command), command
    for command in (
        "gh pr list --repo acme/infra --state all --limit 1000",
        "gh api repos/acme/infra/pulls?state=all",
        "gh api graphql -f query='{ viewer { login } }'",
        "gh api -X GET repos/acme/infra/pulls -f state=all",
        "gh api --method=GET search/issues -f q=repo:acme/infra",
        "gh api graphql -f query='query { viewer { login } }'",
        "gh pr view 3 --comments",
        "gh pr diff 3",
        "gh pr checks 3",
        "gh pr status",
        "gh issue list --state all",
        "gh issue view 4",
        "gh label list",
        "gh release list",
        "gh repo view acme/infra",
        "gh workflow list",
        "gh run list",
        "gh auth status",
        "gh --version",
        "timeout 60 gh pr list --state all",
        'git commit -m "gh pr create was not used"',
    ):
        assert not _flagged(command), command


# The second terminal call of the nightly run that failed the list check
# (ci-kube-agents-eval-nightly 2113057845506215936, rep 3, card t_e6e25baf),
# as typed, and the one line Hermes v2026.9.14 wrote to the card log for it
# (``summarize_shell_command``: the assignment segment is kept as the first
# command, the rest collapses to a count), which carries no list verb.
LISTED_AS_TYPED = (
    "V=/opt/data/profiles/platform/skills/version-control/scripts/vcs.py; "
    "python3 $V proposal list --repo gke-agentic/kube-agents-evals-10-infra --state all -n 100"
    " > /opt/data/scratch/prs_t_e6e25baf.json; echo exit=$?; python3 -c \"\nimport json;"
    "d=json.load(open('/opt/data/scratch/prs_t_e6e25baf.json'));"
    "print({k:v for k,v in d.items() if not isinstance(v,list)})\n\""
)
LISTED_ROW = "V=/opt/data/profiles/platform/skills/version-control/scripts/vcs.py + 2 commands"


def _list_check() -> dict:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == LIST_CHECK]
    return check["check"]


def _listed(check: dict) -> bool:
    call = {
        "name": "terminal",
        "args": {"command": LISTED_AS_TYPED},
        "result": "",
        "status": "error",
        "agent": "platform",
        "task": "t_e6e25baf",
        "session": "s_1",
        "at": 1792657983.68,
    }
    transcript.set("", [call], worker_commands=[{"task": "t_e6e25baf", "command": LISTED_ROW}])
    try:
        result = WorkerCommandsVerifier(**check).verify(5.0)
    finally:
        transcript.clear()
    assert result.status in ("pass", "fail"), result.reason
    return result.success


def test_the_list_check_reads_the_recorded_command_as_typed():
    check = _list_check()
    assert check["after_result_pattern"] == "(?!)"
    assert _listed(check)
    # Without the switch the check reads the card-log row, where the list
    # verb is behind `+ 2 commands`: the recorded false fail (#3128).
    assert not _listed({k: v for k, v in check.items() if k != "after_result_pattern"})
