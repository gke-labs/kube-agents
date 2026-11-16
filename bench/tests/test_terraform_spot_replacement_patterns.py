"""terraform-spot-replacement-pr's reply check accepts the skill's own Replacement wording.

The report_contains verifier ``re.search``es each pattern over
``_normalize_lines``, which deletes underscores with Markdown emphasis, so
``google_container_node_pool.main_pool`` reaches the patterns as
``googlecontainernodepool.mainpool``. A pattern spelled with underscores can
never match. The red run's reply, which opened the pull request and never
mentioned the replacement, must still fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "terraform-spot-replacement-pr" / "task.yaml"
CHECK = "the-reply-names-the-replacement"


def _names_replacement(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return any(re.search(p, _normalize_lines(report)) for p in check["check"]["any_of_patterns"])


def test_replacement_callouts_pass():
    for report in (
        # The skill's Step 5 Replacement line, verbatim in shape.
        "- **Replacement:** REPLACES `google_container_node_pool.main_pool`: `node_config.spot` forces it; "
        "applying this PR destroys and recreates the pool's nodes and their workloads.",
        "this replaces the main_pool node pool",
        "merging this deletes and recreates main-pool",
        "Heads up: `spot = true` forces replacement of the node pool.",
        "The pool will be destroyed and recreated when this is applied.",
    ):
        assert _names_replacement(report), report


def test_a_reply_without_the_replacement_fails():
    for report in (
        # The red run on main (run_20261020_163507_967763), in substance.
        "**Switched the main-pool node pool on cluster gitops-target-1 to Spot VMs by opening a pull request.**\n"
        "Merging this pull request will trigger the declarative GitOps pipeline and update the node pool "
        "configuration for `gitops-target-1`.",
        "I opened PR #9 to set spot = true on main-pool. Merge it to roll out.",
    ):
        assert not _names_replacement(report), report


def test_a_reply_that_denies_the_replacement_fails():
    for report in (
        "This change will not replace the pool.",
        "The node pool is not replaced; spot is updated in place.",
        "Switching to Spot does not force a replacement.",
        "It won't be recreated.",
        "No nodes are recreated; spot is applied in place.",
        "Nothing is destroyed or recreated.",
        "The pool is updated without being replaced.",
        "The pool is not going to be replaced.",
        "It does not need to be replaced.",
    ):
        assert not _names_replacement(report), report


def test_naming_the_alternative_does_not_undo_the_callout():
    assert _names_replacement(
        "REPLACES google_container_node_pool.main_pool: spot = true forces replacement. "
        "Alternative: a new Spot pool beside it, so main-pool would not be replaced."
    )


def _command_checks(case: str) -> tuple[list[str], list[str]]:
    doc = yaml.safe_load((TASK.parent.parent / case / "task.yaml").read_text())
    required, forbidden = [], []
    for check in doc["verification_spec"]:
        if check["check"]["type"] == "worker_commands":
            required += check["check"].get("required_patterns", [])
            forbidden += check["check"].get("forbidden_patterns", [])
    return required, forbidden


CASES = ("terraform-spot-replacement-pr", "terraform-node-pool-autoscaling-pr")


def test_fmt_counts_however_the_command_is_prefixed():
    for case in CASES:
        required, _ = _command_checks(case)
        for command in (
            "tofu fmt -check -diff /opt/data/ws/clusters/gitops-target-1/provisioning",
            "cd /opt/data/ws\ntofu fmt -check -diff .",
            "tofu -chdir=/opt/data/ws fmt",
            'tofu -chdir="/opt/data/a b" fmt',
            "timeout 120 tofu fmt -check -diff .",
            "/usr/local/bin/tofu fmt node_pool.tf",
            "TF_LOG=debug tofu fmt .",
            "bash -c 'tofu fmt .'",
            "if ! tofu fmt -check -diff .; then exit 1; fi",
        ):
            assert all(re.search(p, command) for p in required), (case, command)


def test_a_plan_apply_or_direct_write_is_caught_however_prefixed():
    for case in CASES:
        _, forbidden = _command_checks(case)
        for command in (
            "tofu apply -auto-approve",
            "cd x\ntofu plan",
            "tofu -chdir=x apply",
            'tofu -chdir="a b" apply',
            "timeout 300 tofu apply -auto-approve",
            "env TF_VAR_x=1 tofu apply",
            "/usr/local/bin/terraform apply",
            "gcloud container node-pools update main-pool --cluster gitops-target-1 --enable-autoscaling",
            "gcloud container clusters update gitops-target-1 --enable-autoscaling --node-pool main-pool --max-nodes 3",
            "gcloud container clusters resize gitops-target-1 --node-pool main-pool --num-nodes 3",
        ):
            assert any(re.search(p, command) for p in forbidden), (case, command)


def test_reads_and_fmt_are_not_forbidden():
    for case in CASES:
        _, forbidden = _command_checks(case)
        for command in (
            "tofu fmt -check -diff .",
            "gcloud container node-pools describe main-pool --cluster gitops-target-1",
            "gcloud container clusters describe gitops-target-1 --zone us-central1-a",
            "cat clusters/gitops-target-1/provisioning/node_pool.tf",
            "ls /opt/tofu-plans",
        ):
            assert not any(re.search(p, command) for p in forbidden), (case, command)


def test_a_pr_body_that_names_the_commands_as_prose_is_neither():
    # The skill's Step 5 body, written through a heredoc, names the commands
    # it did not run; a backtick is a code span there, not a command position.
    body = (
        "cat > /tmp/body.md <<'EOF'\n"
        "- **Checked:** `tofu fmt -check` on `clusters/gitops-target-1/provisioning/`. "
        "Not run here: `terraform init`, `validate` and `plan`. Run `plan` in CI before merging.\n"
        "EOF"
    )
    for case in CASES:
        required, forbidden = _command_checks(case)
        assert not any(re.search(p, body) for p in forbidden), case
        assert not all(re.search(p, body) for p in required), case
