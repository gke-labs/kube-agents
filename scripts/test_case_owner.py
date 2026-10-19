"""Tests for scripts/case_owner.py: a case's effective owner."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import textwrap
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import case_owner  # noqa: E402

OWNERS_TEXT = textwrap.dedent(
    """\
    # comment
    approvers:
    - Alice
    - bob  # trailing comment
    reviewers:
    - alice
    - Carol
    options:
      no_parent_owners: true
    """
)

DOMAINS_TEXT = textwrap.dedent(
    """\
    infra_owner: infra-person
    infra_backup: someone-else

    domains:
      - slug: cost
        name: Cost
        owner: Carol
        backup: bob
      - slug: security
        name: Security
        # A comment between keys.
        owner: "alice"
        backup: Carol
    allowlist:
      - cost
    """
)

ROOT = case_owner.parse_root_owners(OWNERS_TEXT)
DOMAINS = case_owner.parse_domain_owners(DOMAINS_TEXT)


class TestParsers(unittest.TestCase):
    def test_root_owners_reads_both_lists_lower_cased(self):
        self.assertEqual(ROOT, {"alice", "bob", "carol"})

    def test_domain_owners_ignore_infra_owner(self):
        self.assertEqual(DOMAINS, {"cost": "Carol", "security": "alice"})

    def test_the_scans_agree_with_yaml_on_the_real_files(self):
        owners = yaml.safe_load(case_owner.OWNERS_FILE.read_text())
        self.assertEqual(
            case_owner.load_root_owners(),
            {login.lower() for key in case_owner.OWNERS_LIST_KEYS for login in owners.get(key) or []},
        )
        domains = yaml.safe_load(case_owner.DOMAINS_FILE.read_text())
        self.assertEqual(
            case_owner.load_domain_owners(),
            {d["slug"]: d["owner"] for d in domains["domains"]},
        )


class TestEffectiveOwner(unittest.TestCase):
    def _owner(self, case, domain="cost"):
        return case_owner.effective_owner(case, domain, root_owners=ROOT, domain_owners=DOMAINS)

    def test_a_listed_case_owner_wins(self):
        self.assertEqual(self._owner("bob"), "bob")

    def test_case_owner_match_is_case_insensitive(self):
        self.assertEqual(self._owner("CAROL", domain="security"), "CAROL")

    def test_an_absent_owner_falls_back_to_the_domain(self):
        self.assertEqual(self._owner(None), "Carol")

    def test_an_owner_not_in_owners_falls_back_to_the_domain(self):
        self.assertEqual(self._owner("departed", domain="security"), "alice")

    def test_maintainers_falls_back_to_the_domain(self):
        self.assertEqual(self._owner("maintainers"), "Carol")

    def test_a_non_string_owner_falls_back_to_the_domain(self):
        self.assertEqual(self._owner(["bob"]), "Carol")

    def test_an_unknown_domain_with_no_valid_owner_is_none(self):
        self.assertIsNone(self._owner(None, domain="no-such-domain"))
        self.assertIsNone(self._owner(None, domain=None))

    def test_retired_and_listed_predicates(self):
        self.assertTrue(case_owner.is_retired_owner(" Maintainers "))
        self.assertFalse(case_owner.is_retired_owner("bob"))
        self.assertTrue(case_owner.is_listed_owner("Bob", ROOT))
        self.assertFalse(case_owner.is_listed_owner(None, ROOT))


class TestEffectiveOwnerOfCase(unittest.TestCase):
    def _checkout(self, task_text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / case_owner.OWNERS_PATH).write_text(OWNERS_TEXT)
        (root / case_owner.DOMAINS_PATH).parent.mkdir(parents=True)
        (root / case_owner.DOMAINS_PATH).write_text(DOMAINS_TEXT)
        task = root / case_owner.TASKS_PATH / "a-case" / case_owner.TASK_FILENAME
        task.parent.mkdir(parents=True)
        task.write_text(task_text)
        return root

    def test_reads_owner_and_domain_from_the_task(self):
        root = self._checkout("id: a-case\ndomain: security\nowner: bob\n")
        self.assertEqual(case_owner.effective_owner_of_case("a-case", root), "bob")

    def test_no_owner_line_uses_the_domain_owner(self):
        root = self._checkout("id: a-case\n# owner: bob in a comment does not count\ndomain: cost\n")
        self.assertEqual(case_owner.effective_owner_of_case("a-case", root), "Carol")

    def test_every_real_case_resolves_to_someone(self):
        for task in sorted((case_owner.REPO_ROOT / case_owner.TASKS_PATH).glob("*/" + case_owner.TASK_FILENAME)):
            with self.subTest(case=task.parent.name):
                self.assertTrue(case_owner.effective_owner_of_case(task.parent.name))


if __name__ == "__main__":
    unittest.main()
