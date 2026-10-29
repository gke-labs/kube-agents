"""Hardening invariants for workflows triggered by `pull_request_target`.

`pull_request_target` runs in the base repository's context with repository
secrets and a writable token scope. Checkout ref safety and `contents`/`id-token`
write ceilings are also covered by
`test_B4_no_pull_request_target_workflow_checks_out_the_pull_request` in
`tests/conformance/test_B_write_path.py`. This suite holds the remaining
workflow-level invariants that neither `test_workflow_pins_and_fork_guards.py`
(which skips `GITHUB_TOKEN`-only workflows) nor `test_merge_group_triggers.py`
(which covers required merge-queue checks only) reaches:

  - Only the four allowlisted workflows declare `pull_request_target`, matching
    the per-workflow repository Actions event rules configured on GitHub.
  - Every `pull_request_target` workflow sets top-level `permissions: {}` so
    each job declares only the scopes it uses.
  - Every job in a `pull_request_target` workflow carries
    `github.repository == 'gke-labs/kube-agents'` in `if:` except
    `merge-gate.yml`'s `gate` job, which runs only on `merge_group` where a
    skipped job would satisfy a required check.
  - Every job in a `pull_request_target` workflow sets an explicit, bounded
    `timeout-minutes` so a stalled HTTP call (`urllib.request.urlopen` has no
    default socket timeout) cannot hang for GitHub's 360-minute default or
    wedge a `cancel-in-progress: false` concurrency group.
  - Every `actions/checkout` step explicitly sets
    `ref: ${{ github.event.repository.default_branch }}` and
    `persist-credentials: false`.
  - No `run:` step interpolates `${{ github.event.* }}`, `${{ github.head_ref }}`,
    `${{ github.base_ref }}`, or `${{ inputs.* }}` directly into shell text;
    untrusted payload fields must cross through `env:`.
"""

from __future__ import annotations

import pathlib
import re
import unittest

from tests.test_merge_group_triggers import _timeout_problem
from tests.test_workflow_pins_and_fork_guards import WORKFLOW_GLOBS, _load, _triggers

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

_PULL_REQUEST_TARGET = "pull_request_target"
_REPO_GUARD = "github.repository == 'gke-labs/kube-agents'"
_DEFAULT_BRANCH_EXPR = "github.event.repository.default_branch"

_ALLOWED_WORKFLOWS = (
    "auto-enqueue.yml",
    "hold-unresolved-threads.yml",
    "merge-gate.yml",
    "risk_classify.yml",
)

# `merge-gate.yml`'s `gate` job runs only on `merge_group`; skipping a required
# check reports success on a queue commit, and forks have no merge queue.
_MERGE_GROUP_ONLY_JOBS = {
    ("merge-gate.yml", "gate"): "github.event_name == 'merge_group'",
}

_EXPRESSION_RE = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
_WHOLE_EXPRESSION_RE = re.compile(r"^\$\{\{(?P<body>.*?)\}\}$", re.DOTALL)
_UNSAFE_RUN_CONTEXT_RE = re.compile(
    r"\b(?:github\.event|github\.head_ref|github\.base_ref|inputs)\b",
    re.IGNORECASE,
)


def _pull_request_target_workflows() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for pattern in WORKFLOW_GLOBS:
        for path in sorted(_WORKFLOWS.glob(pattern)):
            _text, doc = _load(path)
            if doc is not None and _PULL_REQUEST_TARGET in _triggers(doc):
                found[path.name] = doc
    return found


def _is_default_branch_ref(ref: object) -> bool:
    if not isinstance(ref, str):
        return False
    match = _WHOLE_EXPRESSION_RE.match(ref.strip())
    if match is None:
        return False
    # Actions expressions evaluate context paths case-insensitively, but we pin
    # the canonical spelling so a literal `main` or another expression fails.
    return match.group("body").strip() == _DEFAULT_BRANCH_EXPR


def _checkout_step_problems(workflow_name: str, doc: dict) -> tuple[int, list[str]]:
    checkouts = 0
    problems: list[str] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            uses = str((step or {}).get("uses", "")).lower()
            if not uses.startswith("actions/checkout"):
                continue
            checkouts += 1
            step_label = (step or {}).get("name") or f"step #{index + 1}"
            with_block = (step or {}).get("with")
            if not isinstance(with_block, dict):
                problems.append(
                    f"{workflow_name} job {job_id!r} ({step_label}): checkout has no `with:` mapping"
                )
                continue
            ref = with_block.get("ref")
            if not _is_default_branch_ref(ref):
                problems.append(
                    f"{workflow_name} job {job_id!r} ({step_label}): checkout `ref:` is {ref!r}, "
                    f"expected '${{{{ {_DEFAULT_BRANCH_EXPR} }}}}'"
                )
            persist = with_block.get("persist-credentials")
            if persist is not False:
                problems.append(
                    f"{workflow_name} job {job_id!r} ({step_label}): checkout "
                    f"`persist-credentials` is {persist!r}, expected False"
                )
    return checkouts, problems


def _run_step_interpolation_problems(workflow_name: str, doc: dict) -> tuple[int, list[str]]:
    run_steps = 0
    problems: list[str] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            if not isinstance(step, dict) or "run" not in step:
                continue
            run_steps += 1
            step_label = step.get("name") or f"step #{index + 1}"
            run_text = str(step.get("run") or "")
            for expr_match in _EXPRESSION_RE.finditer(run_text):
                body = expr_match.group("body")
                if _UNSAFE_RUN_CONTEXT_RE.search(body):
                    problems.append(
                        f"{workflow_name} job {job_id!r} ({step_label}): `run:` interpolates "
                        f"${{{{{body}}}}}; pass event/input values through `env:`"
                    )
    return run_steps, problems


class PullRequestTargetWorkflowTest(unittest.TestCase):
    def test_only_allowlisted_workflows_declare_pull_request_target(self) -> None:
        self.assertEqual(
            list(_ALLOWED_WORKFLOWS),
            sorted(_pull_request_target_workflows()),
            "unexpected workflow declares pull_request_target; update repository "
            "Actions event policies before adding a new pull_request_target workflow",
        )

    def test_every_pull_request_target_workflow_clears_top_level_permissions(self) -> None:
        for name, doc in _pull_request_target_workflows().items():
            with self.subTest(workflow=name):
                self.assertEqual(
                    {},
                    doc.get("permissions"),
                    f"{name} must set top-level `permissions: {{}}` and scope grants per job",
                )

    def test_every_pull_request_target_job_carries_the_repository_guard(self) -> None:
        for name, doc in _pull_request_target_workflows().items():
            for job_id, job in (doc.get("jobs") or {}).items():
                with self.subTest(workflow=name, job=job_id):
                    condition = str((job or {}).get("if", ""))
                    expected_merge_group_guard = _MERGE_GROUP_ONLY_JOBS.get((name, job_id))
                    if expected_merge_group_guard is not None:
                        self.assertEqual(expected_merge_group_guard, condition.strip())
                    else:
                        self.assertIn(
                            _REPO_GUARD,
                            condition,
                            f"{name} job {job_id!r} runs in a pull_request_target workflow "
                            f"without `{_REPO_GUARD}`",
                        )

    def test_every_pull_request_target_job_sets_a_bounded_timeout(self) -> None:
        for name, doc in _pull_request_target_workflows().items():
            for job_id, job in (doc.get("jobs") or {}).items():
                with self.subTest(workflow=name, job=job_id):
                    self.assertIsNone(
                        _timeout_problem((job or {}).get("timeout-minutes")),
                        f"{name} job {job_id!r}",
                    )

    def test_every_pull_request_target_checkout_pins_default_branch_without_credentials(
        self,
    ) -> None:
        total_checkouts = 0
        for name, doc in _pull_request_target_workflows().items():
            with self.subTest(workflow=name):
                checkouts, problems = _checkout_step_problems(name, doc)
                total_checkouts += checkouts
                self.assertEqual([], problems)
        self.assertGreater(total_checkouts, 0, "expected at least one actions/checkout step")

    def test_no_pull_request_target_run_step_interpolates_event_or_input_expressions(
        self,
    ) -> None:
        total_runs = 0
        for name, doc in _pull_request_target_workflows().items():
            with self.subTest(workflow=name):
                runs, problems = _run_step_interpolation_problems(name, doc)
                total_runs += runs
                self.assertEqual([], problems)
        self.assertGreater(total_runs, 0, "expected at least one run: step")

    def test_checkout_and_run_helpers_reject_unsafe_shapes(self) -> None:
        bad_ref = {
            "jobs": {
                "j": {
                    "steps": [
                        {
                            "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                            "with": {"ref": "main", "persist-credentials": False},
                        }
                    ]
                }
            }
        }
        bad_persist = {
            "jobs": {
                "j": {
                    "steps": [
                        {
                            "uses": "Actions/Checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                            "with": {
                                "ref": "${{ github.event.repository.default_branch }}",
                                "persist-credentials": True,
                            },
                        }
                    ]
                }
            }
        }
        missing_persist = {
            "jobs": {
                "j": {
                    "steps": [
                        {
                            "uses": "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                            "with": {"ref": "${{ github.event.repository.default_branch }}"},
                        }
                    ]
                }
            }
        }
        for label, fixture in (
            ("bad_ref", bad_ref),
            ("bad_persist", bad_persist),
            ("missing_persist", missing_persist),
        ):
            with self.subTest(case=label):
                _, problems = _checkout_step_problems("fixture.yml", fixture)
                self.assertTrue(problems, f"expected {label} to fail checkout check")

        for expr in (
            "${{ github.event.pull_request.title }}",
            "${{ github.Event.label.name }}",
            "${{ github.head_ref }}",
            "${{ inputs.base }}",
        ):
            with self.subTest(expr=expr):
                fixture = {"jobs": {"j": {"steps": [{"run": f"echo {expr}"}]}}}
                _, problems = _run_step_interpolation_problems("fixture.yml", fixture)
                self.assertTrue(problems, f"expected {expr} in run: to fail")


if __name__ == "__main__":
    unittest.main()
