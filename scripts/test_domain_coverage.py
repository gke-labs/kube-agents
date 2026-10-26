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
  has nobody answering for it or no sentence saying what it checks;
* an allowlist entry is a record with ``since``, ``issue`` and ``expires``
  dates and numbers (``owner`` defaults to the domain row's), its expiry at
  most ``MAX_ALLOWLIST_DAYS`` after ``since`` unless ``why`` says why, and its
  optional ``held_out`` case really is a held-out presubmit seat of that
  domain -- so a demotion that uncovers a domain names who brings it back,
  where, and by when;
* an expired record does NOT fail here: a date passing would red unrelated
  pull requests. ``domain_allowlist.expired_records`` is the rule the
  CI-health tick reads to escalate to the record's owner.

Delete allowlist entries as Phase 2 ACTIVATES scenarios. The shrinking
allowlist is the tier-2 progress metric of the testing implementation plan.

Covered means able to red every pull request: a task counts toward its domain
only when its spec is non-empty AND its name is on hack/eval/blocking-roster.txt
(a subset of the presubmit file by construction). A nightly case, or a
presubmit seat held out of the roster, cannot red a pull request on a graded
failure and leaves its domain uncovered. Without this distinction the allowlist would have emptied the day
the specs were written, ten scenarios before any of them ran.
"""

import datetime
import pathlib
import re
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import domain_allowlist  # noqa: E402
import eval_rosters  # noqa: E402
from domain_allowlist import expired_records, record_owner  # noqa: E402
from domain_allowlist import is_date as _is_date  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOMAINS_FILE = REPO_ROOT / "docs" / "designs" / "domains.yaml"
TASKS_GLOB = "bench/tasks/*/task.yaml"


# The longest an allowlist record may run from `since` to `expires` without a
# `why:` saying why (#3968, section 1).
MAX_ALLOWLIST_DAYS = 14
ALLOWLIST_REQUIRED = ("slug", "since", "issue", "expires")
ALLOWLIST_OPTIONAL = ("owner", "why", "held_out")
# As case_owner.GITHUB_LOGIN; restated so this test imports nothing it checks.
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")


def load_domains():
    """``(domain rows, allowlisted slugs)``."""
    data = yaml.safe_load(DOMAINS_FILE.read_text())
    return data["domains"], {
        entry["slug"]
        for entry in data.get("allowlist") or []
        if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
    }


def load_allowlist():
    """The allowlist records as written, not validated."""
    return yaml.safe_load(DOMAINS_FILE.read_text()).get("allowlist") or []


def allowlist_shape_problems(entries) -> list[str]:
    """Every way an allowlist entry falls short of a record; [] when none does."""
    problems = []
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(
                f"entry {index} ({entry!r}): not a record; write `- slug: <domain>` "
                "with since, issue and expires"
            )
            continue
        slug = entry.get("slug")
        name = slug if isinstance(slug, str) and slug else f"entry {index}"
        if not isinstance(slug, str) or not slug:
            problems.append(f"{name}: `slug` must be the domain's slug")
        elif slug in seen:
            problems.append(f"{name}: listed twice")
        else:
            seen.add(slug)
        for key in sorted(set(entry) - set(ALLOWLIST_REQUIRED) - set(ALLOWLIST_OPTIONAL)):
            problems.append(f"{name}: unknown key `{key}`")
        for key in ("since", "expires"):
            if not _is_date(entry.get(key)):
                problems.append(f"{name}: `{key}` must be an unquoted YYYY-MM-DD date, got {entry.get(key)!r}")
        issue = entry.get("issue")
        if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
            problems.append(f"{name}: `issue` must be an issue number, got {issue!r}")
        if "owner" in entry and not (isinstance(entry["owner"], str) and GITHUB_LOGIN.fullmatch(entry["owner"])):
            problems.append(f"{name}: `owner` must be a GitHub login (no at sign), got {entry['owner']!r}")
        for key in ("why", "held_out"):
            if key in entry and not (isinstance(entry[key], str) and entry[key].strip()):
                problems.append(f"{name}: `{key}`, when present, must be non-empty text")
        since, expires = entry.get("since"), entry.get("expires")
        if _is_date(since) and _is_date(expires):
            if expires < since:
                problems.append(f"{name}: `expires` {expires} is before `since` {since}")
            elif (expires - since).days > MAX_ALLOWLIST_DAYS and not (
                isinstance(entry.get("why"), str) and entry["why"].strip()
            ):
                problems.append(
                    f"{name}: `expires` {expires} is {(expires - since).days} days after "
                    f"`since` {since}, over the {MAX_ALLOWLIST_DAYS}-day cap; bring it in, "
                    "or add `why:` saying why it needs longer"
                )
    return problems


def held_out_problems(entries, task_domain, presubmit: set, blocking: set) -> list[str]:
    """A `held_out:` case must be a presubmit seat off the roster that claims the record's domain.

    `task_domain` maps a case name to its task.yaml `domain:` (None when the
    case has no task.yaml).
    """
    problems = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("held_out"), str):
            continue
        slug, case = entry.get("slug"), entry["held_out"]
        if task_domain(case) != slug:
            problems.append(f"{slug}: held_out {case} does not claim domain {slug} in its task.yaml")
        if case not in presubmit:
            problems.append(f"{slug}: held_out {case} is not in hack/eval/presubmit-cases.txt")
        elif case in blocking:
            problems.append(
                f"{slug}: held_out {case} is on hack/eval/blocking-roster.txt, so it covers "
                "the domain; delete the record"
            )
    return problems


def _task_domain(case: str):
    path = REPO_ROOT / "bench" / "tasks" / case / "task.yaml"
    if not path.is_file():
        return None
    return (yaml.safe_load(path.read_text()) or {}).get("domain")


def _root_owners() -> set:
    owners_data = yaml.safe_load((REPO_ROOT / "OWNERS").read_text())
    return {str(login).lower() for key in ("approvers", "reviewers") for login in owners_data.get(key) or []}


def blocking_task_paths():
    """Task paths that can red a pull request: the entries of hack/eval/blocking-roster.txt.

    The roster is a subset of the presubmit file by construction (the script
    refuses a roster name outside it; scripts/test_task_registration.py pins
    it), so a roster case runs on every pull request AND blocks there. A
    presubmit seat held out of the roster (HELD_OUT_IN_PRESUBMIT in
    scripts/test_eval_rosters.py) runs without blocking and covers nothing
    until its roster line lands.
    """
    names = eval_rosters.blocking_roster()
    if not names:
        raise AssertionError(f"{eval_rosters.BLOCKING_ROSTER_FILE} parsed to no cases")
    return {f"bench/tasks/{name}/task.yaml" for name in names}


def covered_domains():
    """Domain slugs claimed by a BLOCKING task carrying a non-empty spec."""
    active = blocking_task_paths()
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
        _, allowlist = load_domains()
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
        listed = _root_owners()
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

    def test_every_allowlist_entry_is_a_record_with_an_issue_and_an_expiry(self):
        problems = allowlist_shape_problems(load_allowlist())
        self.assertEqual(
            problems,
            [],
            "\n\nEvery allowlist entry in docs/designs/domains.yaml is a record "
            "(the comment above `allowlist:` describes the fields):\n  " + "\n  ".join(problems),
        )

    def test_every_allowlist_owner_is_in_owners(self):
        domains, _ = load_domains()
        domain_owners = {d["slug"]: d.get("owner") for d in domains}
        listed = _root_owners()
        departed = sorted(
            f"{entry.get('slug')}: owner {record_owner(entry, domain_owners)}"
            for entry in load_allowlist()
            if isinstance(entry, dict)
            and str(record_owner(entry, domain_owners) or "").lower() not in listed
        )
        self.assertEqual(
            departed,
            [],
            "\n\nThese allowlist owners are not in the root OWNERS file. Name "
            "someone who is, in docs/designs/domains.yaml:\n  " + "\n  ".join(departed),
        )

    def test_a_held_out_seat_on_the_allowlist_is_one(self):
        problems = held_out_problems(
            load_allowlist(),
            _task_domain,
            set(eval_rosters.presubmit_cases()),
            set(eval_rosters.blocking_roster()),
        )
        self.assertEqual(problems, [], "\n\n" + "\n".join(problems))

    def test_the_dashboard_scan_reads_the_same_allowlist(self):
        # scripts/eval_dashboard/collect.py line-scans this file (it stays
        # stdlib-only); a record it cannot see, such as one whose `slug` is not
        # its first key, would drop off the dashboard's coverage count.
        from eval_dashboard import collect

        domains, allowlist = load_domains()
        self.assertEqual(
            collect.coverage(REPO_ROOT),
            {
                "domains_total": len(domains),
                "domains_covered": len(domains) - len(allowlist),
                "uncovered": [e["slug"] for e in load_allowlist()],
            },
            "Start each allowlist record with `- slug: <domain>`, in block style.",
        )



D = datetime.date
RECORD = {"slug": "remediation", "since": D(2026, 10, 17), "issue": 3120, "expires": D(2026, 10, 31)}


class TestAllowlistRules(unittest.TestCase):
    """The record rules on planted entries, with today pinned."""

    def test_a_complete_record_passes(self):
        self.assertEqual(allowlist_shape_problems([RECORD]), [])
        self.assertEqual(allowlist_shape_problems([{**RECORD, "owner": "Fuxiao-Gao", "held_out": "x"}]), [])

    def test_a_bare_slug_is_refused(self):
        self.assertIn("not a record", allowlist_shape_problems(["remediation"])[0])

    def test_each_missing_field_is_named(self):
        for key in ("since", "issue", "expires"):
            with self.subTest(key=key):
                record = {k: v for k, v in RECORD.items() if k != key}
                self.assertTrue(any(f"`{key}`" in p for p in allowlist_shape_problems([record])))

    def test_a_quoted_date_a_timestamp_and_a_hash_issue_are_refused(self):
        self.assertTrue(allowlist_shape_problems([{**RECORD, "expires": "2026-10-31"}]))
        self.assertTrue(allowlist_shape_problems([{**RECORD, "since": datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc)}]))
        self.assertTrue(allowlist_shape_problems([{**RECORD, "issue": "#3120"}]))
        self.assertTrue(allowlist_shape_problems([{**RECORD, "issue": True}]))
        self.assertTrue(allowlist_shape_problems([{**RECORD, "owner": "@someone"}]))

    def test_a_list_or_mapping_slug_is_reported_not_raised(self):
        for slug in (["remediation"], {"name": "remediation"}):
            with self.subTest(slug=slug):
                problems = allowlist_shape_problems([{**RECORD, "slug": slug}, RECORD])
                self.assertTrue(any("`slug` must be" in p for p in problems))

    def test_an_unknown_key_and_a_duplicate_are_refused(self):
        self.assertTrue(allowlist_shape_problems([{**RECORD, "expiry": D(2026, 10, 31)}]))
        self.assertTrue(allowlist_shape_problems([RECORD, RECORD]))

    def test_the_cap_holds_unless_why_says_why(self):
        long = {**RECORD, "expires": RECORD["since"] + datetime.timedelta(days=MAX_ALLOWLIST_DAYS + 1)}
        self.assertIn("cap", allowlist_shape_problems([long])[0])
        self.assertEqual(allowlist_shape_problems([{**long, "why": "Waits on #2013."}]), [])
        self.assertTrue(allowlist_shape_problems([{**long, "why": "  "}]))
        at_cap = {**RECORD, "expires": RECORD["since"] + datetime.timedelta(days=MAX_ALLOWLIST_DAYS)}
        self.assertEqual(allowlist_shape_problems([at_cap]), [])

    def test_expires_before_since_is_refused(self):
        self.assertIn("before", allowlist_shape_problems([{**RECORD, "expires": D(2026, 10, 1)}])[0])

    def test_a_record_expires_the_day_after_its_date_and_names_owner_and_issue(self):
        owners = {"remediation": "Fuxiao-Gao"}
        self.assertEqual(expired_records([RECORD], owners, D(2026, 10, 31)), [])
        self.assertEqual(
            [tuple(r) for r in expired_records([RECORD], owners, D(2026, 11, 3))],
            [("remediation", "Fuxiao-Gao", 3120, D(2026, 10, 31), 3)],
        )
        self.assertEqual(expired_records([{**RECORD, "owner": "lapis2002"}], owners, D(2026, 11, 1))[0].owner, "lapis2002")
        self.assertEqual(expired_records(["remediation", {"slug": "x"}], owners, D(2026, 11, 1)), [])

    def test_an_expired_record_on_the_real_file_is_found_and_does_not_fail_here(self):
        # The presubmit has no expiry test (module docstring); this pins that
        # the rule reads the real file the way the CI-health tick will.
        owners, entries = domain_allowlist.load()
        far = max((e["expires"] for e in entries), default=datetime.date.min) + datetime.timedelta(days=1)
        self.assertEqual([r.slug for r in expired_records(entries, owners, far)], [e["slug"] for e in entries])

    def test_a_held_out_case_must_be_an_off_roster_presubmit_seat_of_the_domain(self):
        entry = {**RECORD, "held_out": "pdb-remediation-pr"}
        domain = {"pdb-remediation-pr": "remediation"}.get
        self.assertEqual(held_out_problems([entry], domain, {"pdb-remediation-pr"}, set()), [])
        self.assertIn("not in", held_out_problems([entry], domain, set(), set())[0])
        self.assertIn("blocking-roster", held_out_problems([entry], domain, {"pdb-remediation-pr"}, {"pdb-remediation-pr"})[0])
        self.assertIn("does not claim", held_out_problems([entry], {}.get, {"pdb-remediation-pr"}, set())[0])


if __name__ == "__main__":
    unittest.main()
