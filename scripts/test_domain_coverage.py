"""A domain with no blocking scenario is uncovered, not passing.

docs/designs/domains.yaml holds the domains and the allowlist of
known-uncovered ones (its header states the rule and where it comes from).
This test is what makes the rule a build failure:

* a domain with no bench task carrying a non-empty ``verification_spec`` must
  be on the allowlist -- otherwise coverage regressed silently;
* a domain that IS covered must not stay on the allowlist -- otherwise the
  list stops being the progress metric it exists to be;
* a task claiming an unknown domain is a typo that would otherwise count as
  coverage of nothing.

Delete allowlist entries as Phase 2 ACTIVATES scenarios. The shrinking
allowlist is the tier-2 progress metric of the testing implementation plan.

Covered means running on every pull request: a task counts toward its domain
only when its spec is non-empty AND its path is an entry in
hack/eval/presubmit-cases.txt, the file hack/ci-eval-pr.sh reads into TASKS.
A case in hack/eval/nightly-cases.txt runs every night and blocks nothing on
a pull request, so a domain whose only scenario is nightly-only is still
uncovered here. Without this distinction the allowlist would have emptied the
day the specs were written, ten scenarios before any of them ran.
"""

import pathlib
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import eval_rosters  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOMAINS_FILE = REPO_ROOT / "docs" / "designs" / "domains.yaml"
TASKS_GLOB = "bench/tasks/*/task.yaml"


def load_domains():
    data = yaml.safe_load(DOMAINS_FILE.read_text())
    return data["domains"], set(data.get("allowlist") or [])


def active_task_paths():
    """Task paths the presubmit runs: the entries of hack/eval/presubmit-cases.txt."""
    names = eval_rosters.presubmit_cases()
    if not names:
        raise AssertionError(f"{eval_rosters.PRESUBMIT_CASES_FILE} parsed to no cases")
    return {f"bench/tasks/{name}/task.yaml" for name in names}


def covered_domains():
    """Domain slugs claimed by an ACTIVE task carrying a non-empty spec."""
    active = active_task_paths()
    claimed = {}
    for task_file in sorted(REPO_ROOT.glob(TASKS_GLOB)):
        task = yaml.safe_load(task_file.read_text()) or {}
        domain = task.get("domain")
        if domain:
            rel = task_file.relative_to(REPO_ROOT).as_posix()
            counts = bool(task.get("verification_spec")) and rel in active
            claimed.setdefault(domain, []).append((rel, counts))
    return claimed


class TestDomainCoverage(unittest.TestCase):
    def test_every_domain_is_covered_or_allowlisted(self):
        domains, allowlist = load_domains()
        claimed = covered_domains()
        uncovered = sorted(
            d["slug"]
            for d in domains
            if d["slug"] not in allowlist
            and not any(has_spec for _, has_spec in claimed.get(d["slug"], []))
        )
        self.assertEqual(
            uncovered,
            [],
            "\n\nThese domains have no task with a verification_spec and are "
            "not on the allowlist in docs/designs/domains.yaml:\n  "
            + "\n  ".join(uncovered)
            + "\n\nAdd a scenario, or add the domain back to the allowlist "
            "with eyes open -- an uncovered domain never counts as passing.",
        )

    def test_a_covered_domain_leaves_the_allowlist(self):
        domains, allowlist = load_domains()
        claimed = covered_domains()
        stale = sorted(
            slug
            for slug in allowlist
            if any(has_spec for _, has_spec in claimed.get(slug, []))
        )
        self.assertEqual(
            stale,
            [],
            "\n\nThese domains are covered but still allowlisted as uncovered; "
            "delete them from docs/designs/domains.yaml so the allowlist stays "
            "the progress metric:\n  " + "\n  ".join(stale),
        )

    def test_no_task_claims_an_unknown_domain(self):
        domains, _ = load_domains()
        known = {d["slug"] for d in domains}
        claimed = covered_domains()
        unknown = sorted(
            f"{files[0][0]}: {slug}"
            for slug, files in claimed.items()
            if slug not in known
        )
        self.assertEqual(
            unknown,
            [],
            "\n\nThese tasks claim a domain docs/designs/domains.yaml does not "
            "define:\n  " + "\n  ".join(unknown),
        )

    def test_every_allowlist_entry_names_a_real_domain(self):
        domains, allowlist = load_domains()
        known = {d["slug"] for d in domains}
        self.assertEqual(
            sorted(allowlist - known),
            [],
            "Allowlist entries must name domains defined in the same file.",
        )


if __name__ == "__main__":
    unittest.main()
