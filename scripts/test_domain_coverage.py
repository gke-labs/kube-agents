"""A domain with no blocking scenario is uncovered, not passing.

docs/designs/domains.yaml holds the domains and the allowlist of
known-uncovered ones (its header states the rule and where it comes from).
This test is what makes the rule a build failure:

* a domain with no bench task carrying a non-empty ``verification_spec`` must
  be on the allowlist -- otherwise coverage regressed silently;
* a domain that IS covered must not stay on the allowlist -- otherwise the
  list stops being the progress metric it exists to be;
* a task claiming an unknown domain is a typo that would otherwise count as
  coverage of nothing;
* a domain with no ``owner:``, ``backup:`` (a second login) or ``meaning:``
  has nobody answering for it or no sentence saying what it checks.

Delete allowlist entries as Phase 2 ACTIVATES scenarios. The shrinking
allowlist is the tier-2 progress metric of the testing implementation plan.

Covered means able to red every pull request: a task counts toward its domain
only when its spec is non-empty AND its name is on hack/eval/blocking-roster.txt
(a subset of the presubmit file by construction). A nightly case, or a
presubmit seat held out of the roster, cannot red a pull request on a graded
failure and leaves its domain uncovered. Without this distinction the allowlist would have emptied the day
the specs were written, ten scenarios before any of them ran.
"""

import pathlib
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import domain_coverage  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOMAINS_FILE = REPO_ROOT / "docs" / "designs" / "domains.yaml"


def load_domains():
    data = yaml.safe_load(DOMAINS_FILE.read_text())
    return data["domains"], set(data.get("allowlist") or [])


def covered_domains():
    """``{domain: [(task path, counts)]}``: ``counts`` when the task is on
    hack/eval/blocking-roster.txt (a subset of the presubmit file by
    construction; a presubmit seat held out of the roster, HELD_OUT_IN_PRESUBMIT
    in scripts/test_eval_rosters.py, covers nothing until its roster line
    lands) and carries a non-empty spec. The CI-health coverage-gap filer
    (scripts/eval_dashboard/coverage_gaps.py) reads coverage through the same
    function (scripts/domain_coverage.py), so the two cannot disagree on what
    covers a domain."""
    return domain_coverage.domain_claims(REPO_ROOT)


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

    def test_every_domain_has_an_owner_a_backup_and_a_meaning(self):
        domains, _ = load_domains()
        missing = sorted(
            f"{d.get('slug')}: {key}"
            for d in domains
            for key in ("owner", "backup", "meaning")
            if not isinstance(d.get(key), str) or not d[key].strip()
        ) + sorted(
            f"{d.get('slug')}: backup is the owner"
            for d in domains
            if isinstance(d.get("owner"), str)
            and isinstance(d.get("backup"), str)
            and d["owner"].strip().lower() == d["backup"].strip().lower()
        )
        self.assertEqual(
            missing,
            [],
            "\n\nEvery domain in docs/designs/domains.yaml needs a non-empty "
            "`owner:` and `backup:` (two different GitHub logins) and a "
            "`meaning:` (one plain sentence):\n  " + "\n  ".join(missing),
        )

    def test_every_domain_owner_and_backup_is_in_owners(self):
        # A case with no `owner:` routes to its domain's owner, and a filer
        # whose assignee is refused falls back to it, so it must be someone
        # still here: listed in the root OWNERS file.
        owners_data = yaml.safe_load((REPO_ROOT / "OWNERS").read_text())
        listed = {str(login).lower() for key in ("approvers", "reviewers") for login in owners_data.get(key) or []}
        domains, _ = load_domains()
        departed = sorted(
            f"{d.get('slug')}: {key} {d[key]}"
            for d in domains
            for key in ("owner", "backup")
            if isinstance(d.get(key), str) and d[key].strip().lower() not in listed
        )
        self.assertEqual(
            departed,
            [],
            "\n\nThese domain owners and backups are not in the root OWNERS "
            "file. Name someone who is, in docs/designs/domains.yaml:\n  " + "\n  ".join(departed),
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
