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

DOMAINS = case_owner.parse_domain_owners(DOMAINS_TEXT)


class TestParsers(unittest.TestCase):
    def test_domain_owners_ignore_infra_owner(self):
        self.assertEqual(DOMAINS, {"cost": "Carol", "security": "alice"})

    def test_the_scan_agrees_with_yaml_on_the_real_file(self):
        domains = yaml.safe_load(case_owner.DOMAINS_FILE.read_text())
        self.assertEqual(
            case_owner.load_domain_owners(),
            {d["slug"]: d["owner"] for d in domains["domains"]},
        )


class TestEffectiveOwner(unittest.TestCase):
    def _owner(self, case, domain="cost"):
        return case_owner.effective_owner(case, domain, domain_owners=DOMAINS)

    def test_a_case_owner_wins_whether_or_not_in_owners(self):
        # Assignability is the filer's check, at bug-filing time.
        self.assertEqual(self._owner("bob"), "bob")
        self.assertEqual(self._owner("someone-outside-owners", domain="security"), "someone-outside-owners")

    def test_an_absent_owner_falls_back_to_the_domain(self):
        self.assertEqual(self._owner(None), "Carol")

    def test_maintainers_falls_back_to_the_domain(self):
        for owner in ("maintainers", "Maintainers"):
            with self.subTest(owner=owner):
                self.assertEqual(self._owner(owner), "Carol")

    def test_a_non_login_owner_falls_back_to_the_domain(self):
        for owner in (["bob"], "", "@bob", "some one", "a@b.com"):
            with self.subTest(owner=owner):
                self.assertEqual(self._owner(owner), "Carol")

    def test_an_unknown_domain_with_no_case_owner_is_none(self):
        self.assertIsNone(self._owner(None, domain="no-such-domain"))
        self.assertIsNone(self._owner(None, domain=None))

    def test_domain_owner_is_the_refusal_fallback(self):
        self.assertEqual(case_owner.domain_owner("security", DOMAINS), "alice")
        self.assertIsNone(case_owner.domain_owner(["security"], DOMAINS))

    def test_predicates(self):
        self.assertTrue(case_owner.is_retired_owner(" Maintainers "))
        self.assertFalse(case_owner.is_retired_owner("bob"))
        self.assertTrue(case_owner.is_person_login("GinnyJI"))
        self.assertFalse(case_owner.is_person_login("maintainers"))
        self.assertFalse(case_owner.is_person_login(None))


class TestEffectiveOwnerOfCase(unittest.TestCase):
    def _checkout(self, task_text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / case_owner.DOMAINS_PATH).parent.mkdir(parents=True)
        (root / case_owner.DOMAINS_PATH).write_text(DOMAINS_TEXT)
        task = root / case_owner.TASKS_PATH / "a-case" / case_owner.TASK_FILENAME
        task.parent.mkdir(parents=True)
        task.write_text(task_text)
        return root

    def test_reads_owner_and_domain_from_the_task(self):
        root = self._checkout("id: a-case\ndomain: security\nowner: outsider\n")
        self.assertEqual(case_owner.effective_owner_of_case("a-case", root), "outsider")

    def test_no_owner_line_uses_the_domain_owner(self):
        root = self._checkout("id: a-case\n# owner: bob in a comment does not count\ndomain: cost\n")
        self.assertEqual(case_owner.effective_owner_of_case("a-case", root), "Carol")

    def test_a_missing_or_hostile_case_name_is_none(self):
        root = self._checkout("id: a-case\nowner: bob\ndomain: cost\n")
        for name in ("renamed-case", "../a-case", "..", "a-case/../a-case", "", None):
            with self.subTest(name=name):
                self.assertIsNone(case_owner.effective_owner_of_case(name, root))

    def test_a_blank_owner_line_uses_the_domain_owner(self):
        root = self._checkout("id: a-case\nowner:\ndomain: security\n")
        self.assertEqual(case_owner.effective_owner_of_case("a-case", root), "alice")

    def test_the_scan_agrees_with_yaml_on_every_real_case(self):
        # The validator reads task.yaml as YAML; the filer's helper line-scans
        # it. A spelling only one of them understands would route reds to the
        # domain owner while the lint shows the case owner. Field by field, not
        # through effective_owner: on a case whose owner is also its domain's
        # owner, a misread owner falls back to the same login and hides.
        for task in sorted((case_owner.REPO_ROOT / case_owner.TASKS_PATH).glob("*/" + case_owner.TASK_FILENAME)):
            text = task.read_text()
            spec = yaml.safe_load(text)
            with self.subTest(case=task.parent.name):
                self.assertEqual(case_owner.task_field(text, "owner"), spec.get("owner"))
                self.assertEqual(case_owner.task_field(text, "domain"), spec.get("domain"))

    def test_every_real_case_resolves_to_someone(self):
        for task in sorted((case_owner.REPO_ROOT / case_owner.TASKS_PATH).glob("*/" + case_owner.TASK_FILENAME)):
            with self.subTest(case=task.parent.name):
                self.assertTrue(case_owner.effective_owner_of_case(task.parent.name))


if __name__ == "__main__":
    unittest.main()
