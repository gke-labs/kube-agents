"""Which bench cases claim which testing domain, and which of them cover it.

A case covers its domain (``domain:`` in its task.yaml, one of the slugs in
docs/designs/domains.yaml) when it carries a non-empty ``verification_spec``
AND its name is on hack/eval/blocking-roster.txt: covered means able to red
every pull request. scripts/test_domain_coverage.py holds the allowlist to
this, and the CI-health coverage-gap filer (scripts/eval_dashboard/
coverage_gaps.py) reads it to find the uncovered domains, so the two cannot
disagree. Kept apart from both so the lint imports nothing of the CI-health
stack.
"""

from __future__ import annotations

import pathlib

import eval_rosters
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS_GLOB = "bench/tasks/*/task.yaml"
BLOCKING_ROSTER_PATH = pathlib.Path("hack") / "eval" / "blocking-roster.txt"


def task_claims(repo_root: pathlib.Path = REPO_ROOT) -> list[tuple[str, str, bool]]:
    """``[(task path, domain, has a non-empty verification_spec)]`` for every
    bench task claiming a domain, in path order."""
    out = []
    for task_file in sorted(repo_root.glob(TASKS_GLOB)):
        task = yaml.safe_load(task_file.read_text()) or {}
        domain = task.get("domain")
        if domain:
            out.append((task_file.relative_to(repo_root).as_posix(), domain, bool(task.get("verification_spec"))))
    return out


def domain_claims(repo_root: pathlib.Path = REPO_ROOT) -> dict[str, list[tuple[str, bool]]]:
    """``{domain: [(task path, counts)]}``: ``counts`` when the task carries
    a spec and is on the blocking roster (a subset of the presubmit file by
    construction; a held-out presubmit seat covers nothing). Raises when the
    roster parses to no cases."""
    names = eval_rosters.blocking_roster(repo_root / BLOCKING_ROSTER_PATH)
    if not names:
        raise AssertionError(f"{repo_root / BLOCKING_ROSTER_PATH} parsed to no cases")
    active = {f"bench/tasks/{name}/task.yaml" for name in names}
    claimed: dict[str, list[tuple[str, bool]]] = {}
    for path, domain, has_spec in task_claims(repo_root):
        claimed.setdefault(domain, []).append((path, has_spec and path in active))
    return claimed
