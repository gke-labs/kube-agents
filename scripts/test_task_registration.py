"""Every bench case is valid, and in a roster file under hack/eval/ or excluded.

`hack/ci-eval-pr.sh` runs the cases named in hack/eval/presubmit-cases.txt
and, under EVAL_TIER=nightly, hack/eval/nightly-cases.txt -- and only those:
tasks under bench/tasks/ are not picked up automatically, the files' headers
say so, and nothing owned the difference between "left out on purpose" and
"nobody remembered". That is how agent-kanban-smoke -- a task whose whole
point is to smoke the deployed pipeline -- sat registered nowhere while the
presubmit ran one task for months. A task nobody registered is the same
failure as a domain nobody covered.

This test owns that difference, and four more like it. The rules and the
allowlists live in scripts/validate_bench_cases.py, which `make
bench-case-check` also runs. Sharing the implementation is only half of what
it takes for the two to agree: this lint also has to assert on everything that
module returns rather than on a hand-listed set of substrings, which is what
TestEveryTaskIsValid's whole-set assertion is for -- see its docstring for the
rules that leaked through before it existed. `make bench-case-check` is
invoked by no workflow; this lint, reached through PYTHON_TEST_DIRS in the
Makefile and run by .github/workflows/python-tests.yml, is the whole of the
enforcement on a pull request. A case passes by an entry in the presubmit
file, by an entry in the nightly file (where a new case lands by default and
earns a presubmit seat on its record -- decided 2026-09-15 on #1546/#1564),
by a FIXTURE_NOT_READY entry in the validator naming the issue that plants
its fixture, or by a reviewed KNOWN_UNREGISTERED entry with the reason. The
commented-out registration the script's TASKS array used to accept is
retired: a case path inside a roster-file comment is a finding.

This lint and scripts/test_domain_coverage.py ratchet together. That one
counts a domain covered only when a task carrying its slug and a non-empty
verification_spec is on the BLOCKING ROSTER, so neither a nightly-only task
nor a held-out presubmit seat counts as coverage; this one guarantees every
task is at least registered
and internally valid.

The files are read with the parse in scripts/eval_rosters.py, the Python copy
of the sed the script applies; scripts/test_eval_rosters.py holds the two to
the same answer. A parse that finds nothing fails loudly rather than passing
vacuously.
"""

import contextlib
import io
import pathlib
import re
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import validate_bench_cases as validator  # noqa: E402

# Sentinel for "this case omits the key entirely", which None cannot express:
# `domain: null` and no `domain:` line are the same thing to yaml.safe_load,
# but a test needs to say which one it means.
DELETE = object()

import eval_rosters  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestEveryTaskIsRegistered(unittest.TestCase):
    def test_every_task_is_in_presubmit_or_nightly_or_excluded_by_name(self):
        registered = validator.registered_cases()
        self.assertIsNotNone(
            registered,
            "Could not read hack/eval/presubmit-cases.txt and nightly-cases.txt "
            "as case lists -- a file is missing or holds a malformed line.",
        )
        excluded = set(validator.KNOWN_UNREGISTERED) | set(validator.FIXTURE_NOT_READY)
        orphans = sorted(
            name for name in validator.bench_cases() if name not in registered and name not in excluded
        )
        self.assertEqual(
            orphans,
            [],
            "\n\nThese bench tasks are registered nowhere and never run:\n  "
            + "\n  ".join(orphans)
            + "\n\nAdd each to hack/eval/nightly-cases.txt (where a new case "
            "lands), or to FIXTURE_NOT_READY in scripts/validate_bench_cases.py "
            "with the issue that plants its fixture, or to KNOWN_UNREGISTERED "
            "there with the reason it must not run.",
        )

    def test_every_task_is_in_exactly_one_place(self):
        # Presubmit, nightly, FIXTURE_NOT_READY and KNOWN_UNREGISTERED are
        # four answers to one question; a case with two of them has a stale
        # one. The shell rejects the presubmit/nightly overlap itself (it
        # would run the case twice a night); the allowlist overlaps are the
        # validator's stale sweep, asserted here as one set rule.
        homes = {
            "presubmit-cases.txt": set(eval_rosters.presubmit_cases()),
            "nightly-cases.txt": set(eval_rosters.nightly_cases()),
            "FIXTURE_NOT_READY": set(validator.FIXTURE_NOT_READY),
            "KNOWN_UNREGISTERED": set(validator.KNOWN_UNREGISTERED),
        }
        seen: dict[str, list[str]] = {}
        for label, names in homes.items():
            for name in names:
                seen.setdefault(name, []).append(label)
        doubled = {name: where for name, where in seen.items() if len(where) > 1}
        self.assertEqual(doubled, {}, "\n\nThese cases are named in more than one place:")

    def test_no_case_is_parked_as_a_comment(self):
        # The retired state: a `# ./tasks/<id>/task.yaml` line. It was how
        # scenarios waited for the seeded fleet, and it was indistinguishable
        # from a case nobody had decided about.
        self.assertEqual(
            validator.commented_out_registrations(),
            {},
            "\n\nThese cases are commented out in a roster file. Move each to "
            "hack/eval/nightly-cases.txt, or to FIXTURE_NOT_READY with its issue.",
        )

    def test_every_fixture_not_ready_entry_names_its_issue(self):
        self.assertEqual(
            validator.fixture_not_ready_without_issue(),
            [],
            "\n\nThese FIXTURE_NOT_READY reasons name no issue (#<number>); the "
            "entry is a wait on something, and the issue is what it waits on.",
        )

    def test_the_blocking_roster_is_a_subset_of_the_presubmit(self):
        # The shell refuses to start otherwise; this is the same rule where
        # it costs a second rather than a cluster lease.
        roster = set(eval_rosters.blocking_roster())
        self.assertTrue(roster, "hack/eval/blocking-roster.txt parsed to no cases")
        outside = sorted(roster - set(eval_rosters.presubmit_cases()))
        self.assertEqual(
            outside,
            [],
            "\n\nThese blocking-roster entries are not presubmit cases; a case "
            "cannot block a pull request it does not run on:\n  " + "\n  ".join(outside),
        )

    def test_the_exclusion_lists_do_not_rot(self):
        # An entry whose task directory is gone is stale noise. Entries whose
        # task has since been registered are pruned in review, not enforced
        # here -- an in-flight branch registering a task must not red main the
        # day it merges.
        stale = validator.stale_allowlist_entries()
        self.assertEqual(
            stale,
            [],
            "\n\nThese allowlist entries in scripts/validate_bench_cases.py "
            "match no bench task any more; delete them:\n  " + "\n  ".join(stale),
        )

    def test_the_parse_reads_nonempty_files(self):
        # If the roster parse ever comes back empty the first test would call
        # every task an orphan; fail with the real story instead.
        self.assertTrue(
            validator.registered_cases(),
            "hack/eval/presubmit-cases.txt and nightly-cases.txt parsed to no "
            "cases -- either both are empty or the validator's parse has drifted.",
        )

    def test_a_nightly_entry_counts_as_registered(self):
        # The property scripts/test_ci_eval_nightly.py's tier tests stand on:
        # a case listed only in the nightly file is registered, not an orphan.
        nightly = set(eval_rosters.nightly_cases())
        self.assertTrue(nightly, "hack/eval/nightly-cases.txt parsed to no entries")
        registered = validator.registered_cases()
        self.assertTrue(
            nightly <= registered,
            f"nightly entries missing from registered_cases(): "
            f"{sorted(nightly - registered)} -- the validator's parse has drifted.",
        )


class TestEveryTaskIsValid(unittest.TestCase):
    """The rest of the case contract: docs/designs/bench-case-format.md.

    Two layers, and they do different jobs.
    test_no_case_is_rejected_for_any_reason is the gate: it asserts the whole
    result set is empty, so every rule the validator has reds a pull request,
    including the ones written after this file was last read. The per-rule
    assertions under it are the diagnosis: each matches one substring, so a
    failure names which part of the contract broke instead of dumping every
    finding at once.

    The per-rule layer was the whole gate for one commit, and it leaked. Every
    assertion here matches a fixed substring of a problem string, so a problem
    matching none of them was collected into cls.results and never asserted
    on: `does not parse to a mapping`, `declares no 'id:'`, `duplicate entry
    name`, an unknown `severity:` value, `check node has no 'type'
    discriminator` and eight more were rejected by `make bench-case-check` and
    would have merged green -- the cluster-lease cost this file exists to
    remove. A needle list is a hand-maintained second copy of the validator's
    rule set, and it drifts the first time somebody adds a rule without
    editing this file. The whole-set assertion cannot drift, so it is the one
    that gates.
    """

    @classmethod
    def setUpClass(cls):
        cls.results = validator.validate_all()

    def _findings(self, needle):
        return sorted(
            f"{name}: {problem}"
            for name, problems in self.results.items()
            for problem in problems
            if needle in problem
        )

    def _assert_none(self, needle, guidance):
        found = self._findings(needle)
        self.assertEqual(found, [], "\n\n" + "\n  ".join([guidance, *found]))

    def test_no_case_is_rejected_for_any_reason(self):
        # The gate. Everything below names one rule; this one covers the set,
        # so a validator rule with no assertion of its own still fails here.
        # Keep it even when a per-rule assertion looks like it subsumes a
        # finding -- the point is the rules nobody has written yet.
        findings = sorted(
            f"{name}: {problem}"
            for name, problems in self.results.items()
            for problem in problems
        )
        self.assertEqual(
            findings,
            [],
            "\n\nThese bench cases break the contract in "
            "docs/designs/bench-case-format.md. `make bench-case-check` "
            "prints the same findings against your working tree:\n  "
            + "\n  ".join(findings),
        )

    def test_no_task_uses_the_deprecated_id_key(self):
        self._assert_none(
            "task_id",
            "These cases use the deprecated 'task_id:' key. devops-bench "
            "accepts it as an alias and prefers 'id:' when both are present, "
            "so renaming is a no-op at runtime and the tree keeps one "
            "spelling:",
        )

    def test_every_task_id_matches_its_directory(self):
        self._assert_none(
            "does not match its directory name",
            "These cases disagree with their own directory name, which is "
            "what the roster files, the results file and every lint key on:",
        )

    def test_every_task_claims_a_known_domain(self):
        for needle, guidance in (
            (
                "declares no 'domain:'",
                "These cases claim no domain, so they cover nothing in the "
                "coverage report while looking green:",
            ),
            (
                "which docs/designs/domains.yaml does not define",
                "These cases claim a domain slug that does not exist:",
            ),
        ):
            with self.subTest(rule=needle):
                self._assert_none(needle, guidance)

    def test_every_task_names_an_owner(self):
        for needle, guidance in (
            (
                "declares no 'owner:'",
                "These cases name nobody to answer when they flake -- see "
                "bench/CONTRIBUTING.md:",
            ),
            ("leading at sign", "These cases write the owner as a mention:"),
            ("neither a GitHub login", "These cases carry an owner that is not a login:"),
        ):
            with self.subTest(rule=needle):
                self._assert_none(needle, guidance)

    def test_every_expected_fail_marker_is_a_boolean(self):
        self._assert_none(
            "is not a YAML boolean",
            "These cases carry an 'expected_fail:' that is not a bare true or "
            "false; bench-gate would refuse it after the cluster lease:",
        )

    def test_no_fixture_carries_a_real_address_or_credential(self):
        # The sanitization scan is tree-level rather than per case, like the
        # fixture-catalogue drift check, so validate_all() does not carry it
        # and this is the assertion that gates it.
        findings = validator.sanitization_findings()
        self.assertEqual(
            findings,
            [],
            "\n\nThese fixture lines carry an IPv4 literal outside the RFC 5737 "
            "documentation ranges or a credential-shaped string. Replace the "
            "value, or append 'sanitizer: allow <reason>' to the line -- see "
            "bench/CONTRIBUTING.md:\n  " + "\n  ".join(findings),
        )

    def test_every_named_fixture_role_exists(self):
        self._assert_none(
            "neither bench/tf/fleet/fixtures.json",
            "These cases name a seeded-fleet fixture role no catalogue "
            "defines. Cases address fixtures by role, never by cluster "
            "name -- see docs/designs/bench-fleet-catalog.md:",
        )

    def test_every_role_a_check_names_is_declared_by_its_case(self):
        self._assert_none(
            "which the case's own 'fixtures:' list does not declare",
            "A check's `fixture_role:` and the case's `fixtures:` list name "
            "the same planted defect and must use the same slug -- see "
            "docs/designs/fleet-fixtures.yaml's header:",
        )

    def test_every_task_carries_a_verification_spec(self):
        self._assert_none(
            "carries no 'verification_spec:'",
            "These cases are judge-only. The OutcomeValidity >= 0.7 fallback "
            "in hack/ci-eval-pr.sh is transitional:",
        )

    def test_every_entry_uses_the_vocabulary_devops_bench_accepts(self):
        # Each of these is rejected by VerificationEntry at spec-load time,
        # which is a parse error worth 1.0 on the objective denominator -- a
        # red presubmit discovered after a cluster lease rather than here.
        for needle, guidance in (
            ("is not one of ['objective', 'safeguard']", "These entries declare no usable role:"),
            ("a safeguard must declare a severity", "These safeguards declare no severity:"),
            ("on an objective", "These objectives declare a severity, which is for safeguards:"),
            ("is not one of ['assert', 'converge']", "These entries name an unbuilt mode:"),
            ("must be a number greater than 0", "These entries carry a non-positive weight:"),
        ):
            with self.subTest(rule=needle):
                self._assert_none(needle, guidance)

    def test_every_cluster_reading_case_declares_its_fixtures(self):
        self._assert_none(
            "declares no 'fixtures:'",
            "These cases assert on live cluster state without saying what "
            "has to be there:",
        )

    def test_every_spec_is_visible_to_the_presubmit(self):
        self._assert_none(
            "inline rather than as a block",
            "These cases declare a spec the gate cannot see, so they run their "
            "checks and are graded by the judge-only fallback anyway:",
        )

    def test_no_check_is_unfailable(self):
        for needle, guidance in (
            (
                "so it can only pass",
                "These checks populate no assertion field and pass whatever "
                "the run did:",
            ),
            (
                "asserts nothing",
                "These compound checks have no members:",
            ),
            (
                "unknown check type",
                "These checks name a verifier nothing registers:",
            ),
            (
                "has no 'check:' subtree",
                "These verification entries declare no check at all:",
            ),
        ):
            with self.subTest(rule=needle):
                self._assert_none(needle, guidance)


class TestTheValidatorItself(unittest.TestCase):
    """The catalogues the validator reads are the ones the tree ships."""

    def test_the_local_verifier_types_match_the_entry_points(self):
        # bench/pyproject.toml is the sole registration path for this
        # repository's verifiers. A new one added there without a
        # CHECK_ASSERTIONS entry would be reported as an unknown type on the
        # first case that used it; fail here instead, where the fix is
        # obvious.
        text = (REPO_ROOT / "bench" / "pyproject.toml").read_text()
        block = re.search(
            r'^\[project\.entry-points\."devops_bench\.verifiers"\]\n(.*?)(?=^\[|\Z)',
            text,
            re.M | re.S,
        )
        self.assertIsNotNone(
            block,
            "bench/pyproject.toml declares no devops_bench.verifiers "
            "entry-point group -- the registration path moved.",
        )
        declared = set(re.findall(r"^([a-z_]+)\s*=", block.group(1), re.M))
        missing = sorted(declared - set(validator.CHECK_ASSERTIONS))
        self.assertEqual(
            missing,
            [],
            "\n\nThese verifiers are registered in bench/pyproject.toml but "
            "have no CHECK_ASSERTIONS entry in "
            "scripts/validate_bench_cases.py, so the validator cannot tell "
            "whether a check using them asserts anything:\n  "
            + "\n  ".join(missing),
        )

    def test_the_fixture_catalog_loads(self):
        roles = validator.known_fixture_roles()
        self.assertTrue(
            roles,
            "no fixture roles are defined -- either "
            "bench/tf/fleet/fixtures.json or docs/designs/fleet-fixtures.yaml "
            "moved, or its shape changed.",
        )

    def test_the_two_fixture_catalogues_agree(self):
        # bench/tf/fleet/fixtures.json owns the role vocabulary and
        # docs/designs/fleet-fixtures.yaml overlays the day-N gates on it. The
        # two once disagreed on five of eight slugs, which put two names for
        # one planted defect in the same task.yaml.
        self.assertEqual(
            validator.fixture_catalog_disagreements(),
            [],
            "\n\ndocs/designs/fleet-fixtures.yaml has drifted from "
            "bench/tf/fleet/fixtures.json:\n  "
            + "\n  ".join(validator.fixture_catalog_disagreements()),
        )

    def test_a_case_with_no_assertion_is_rejected(self):
        problems = []
        validator._check_assertions(
            {"type": "report_contains", "scope": "final"}, "check 'x'", problems
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("can only pass", problems[0])

    def test_a_check_that_asserts_without_a_field_is_not_rejected(self):
        # An empty CHECK_ASSERTIONS tuple is the "no field can switch this off"
        # case, not a missing entry: pull_request_opened fails on a report
        # naming no pull request with nothing configured. Read as a missing
        # entry it would reject every case using one.
        problems = []
        validator._check_assertions(
            {"type": "pull_request_opened"}, "check 'x'", problems
        )
        self.assertEqual(problems, [])

    def test_main_exits_non_zero_when_a_case_is_rejected(self):
        # Every other test here reads the problem list that validate_case
        # returns. None of them runs main(), so none of them would notice if
        # main() found problems, printed them, and returned 0 anyway --
        # `make bench-case-check` would go green on a tree it had just
        # rejected, which is the one failure that makes the whole target
        # decorative. Assert the exit code, not the report.
        spec = {
            "id": "made-up-case",
            "name": "A case",
            "domain": "security",
            "owner": "maintainers",
            "fixtures": ["rbac-overgrant"],
            "verification_spec": [
                {
                    "name": "names-the-thing",
                    "role": "objective",
                    "check": {"type": "report_contains", "required_phrases": ["x"]},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "made-up-case" / "task.yaml"
            path.parent.mkdir()

            path.write_text(yaml.safe_dump(spec))
            with contextlib.redirect_stdout(io.StringIO()) as clean:
                self.assertEqual(
                    validator.main([str(path)]),
                    0,
                    "a case the validator accepts must exit 0",
                )
            self.assertIn("OK", clean.getvalue())

            # One rule broken -- an unknown domain -- is enough. Which rule
            # does not matter; that main() propagates any finding does.
            path.write_text(yaml.safe_dump({**spec, "domain": "not-a-real-domain"}))
            with contextlib.redirect_stdout(io.StringIO()) as dirty:
                self.assertEqual(
                    validator.main([str(path)]),
                    1,
                    "a rejected case must exit non-zero or bench-case-check "
                    "reports success on a broken tree",
                )
            self.assertIn("rejected", dirty.getvalue())


class TestTheRulesReject(unittest.TestCase):
    """Every rule, against a case built to break exactly that rule.

    The suite above proves the tree is clean, which a validator that found
    nothing at all would also prove. These prove it finds things, and the
    `_only` helper proves it finds one thing: a rule that fires on everything
    is as useless as a rule that fires on nothing, and it would make every
    other test here pass for the wrong reason.
    """

    VALID = {
        "id": "made-up-case",
        "name": "A case",
        "domain": "security",
        "owner": "maintainers",
        "fixtures": ["rbac-overgrant"],
        "verification_spec": [
            {
                "name": "names-the-thing",
                "role": "objective",
                "check": {"type": "report_contains", "required_phrases": ["x"]},
            }
        ],
    }

    def _validate(self, *, registered=frozenset({"made-up-case"}), text=None, **overrides):
        spec = {k: v for k, v in {**self.VALID, **overrides}.items() if v is not DELETE}
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "made-up-case" / "task.yaml"
            path.parent.mkdir()
            path.write_text(text if text is not None else yaml.safe_dump(spec))
            return validator.validate_case("made-up-case", path, registered=set(registered))

    def _only(self, needle, **kwargs):
        """The one problem this mutation causes, and nothing else."""
        problems = self._validate(**kwargs)
        self.assertEqual(len(problems), 1, f"expected one problem, got {problems}")
        self.assertIn(needle, problems[0])
        return problems[0]

    def _entry(self, **overrides):
        entry = {**self.VALID["verification_spec"][0], **overrides}
        return [{k: v for k, v in entry.items() if v is not DELETE}]

    def test_the_valid_case_passes(self):
        self.assertEqual(self._validate(), [])

    # -- the case-level keys --

    def test_the_task_id_alias_is_rejected(self):
        self._only("deprecated 'task_id:'", id=DELETE, task_id="made-up-case")

    def test_a_case_with_no_id_is_rejected(self):
        self._only("declares no 'id:'", id=DELETE)

    def test_an_id_that_disagrees_with_its_directory_is_rejected(self):
        self._only("does not match its directory name", id="some-other-name")

    def test_a_missing_domain_is_rejected(self):
        self._only("declares no 'domain:'", domain=DELETE)

    def test_an_unknown_domain_is_rejected(self):
        self._only("docs/designs/domains.yaml does not define", domain="not-a-domain")

    def test_a_domain_that_is_not_a_string_is_rejected(self):
        # Membership of a set of slugs raises TypeError on an unhashable
        # value, which would be a traceback instead of a finding.
        self._only("is not a slug string", domain=["security", "cost"])

    def test_a_missing_owner_is_rejected(self):
        self._only("declares no 'owner:'", owner=DELETE)

    def test_an_owner_with_a_leading_at_sign_is_rejected(self):
        self._only("leading at sign", owner="@someone")

    def test_a_malformed_owner_is_rejected(self):
        # An e-mail address, a display name and a doubled hyphen are the three
        # shapes a contributor is likely to write; GitHub accepts none of them.
        for owner in ("someone@example.com", "Some One", "some--one", "-someone"):
            with self.subTest(owner=owner):
                self._only("neither a GitHub login", owner=owner)

    def test_an_owner_that_is_not_a_string_is_rejected(self):
        self._only("is not a GitHub login string", owner=["a", "b"])

    def test_a_login_owner_passes(self):
        self.assertEqual(self._validate(owner="some-one1"), [])

    def test_a_quoted_expected_fail_is_rejected(self):
        # yaml.safe_dump quotes a string that would otherwise read as a bool,
        # which is exactly the file a contributor produces by typing quotes.
        for literal in ("false", "true", "yes"):
            with self.subTest(literal=literal):
                self._only("is not a YAML boolean", expected_fail=literal)

    def test_an_expected_fail_that_is_not_a_boolean_is_rejected(self):
        for value in (1, 0, ["true"]):
            with self.subTest(value=value):
                self._only("is not a YAML boolean", expected_fail=value)

    def test_a_bare_boolean_expected_fail_passes(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.assertEqual(self._validate(expected_fail=value), [])

    def test_an_unknown_fixture_role_is_rejected(self):
        # Deliberately not a plausible-looking slug: `hpa-saturated` used to
        # sit here and became a real role the day bench/tf/fleet/fixtures.json
        # merged, which turned this into a test that asserted nothing.
        self._only("neither bench/tf/fleet/fixtures.json", fixtures=["no-such-fixture"])

    def test_a_fixture_role_that_is_not_a_string_is_rejected(self):
        self._only("is not a slug string", fixtures=[["rbac-overgrant"]])

    def test_a_fixtures_value_that_is_not_a_list_is_rejected(self):
        self._only("must be a list of role slugs", fixtures="rbac-overgrant")

    def test_an_unregistered_case_is_rejected(self):
        self._only("registered nowhere", registered=frozenset())

    # -- the verification spec --

    def test_a_missing_verification_spec_is_rejected(self):
        self._only("carries no 'verification_spec:'", verification_spec=DELETE)

    def test_a_verification_spec_that_is_not_a_list_is_rejected(self):
        self._only("must be a list of entries", verification_spec={"name": "n"})

    def test_a_cluster_reading_case_with_no_fixtures_is_rejected(self):
        self._only(
            "declares no 'fixtures:'",
            fixtures=DELETE,
            verification_spec=self._entry(
                check={
                    "type": "resource_property",
                    "kind": "deployment",
                    "resource_name": "x",
                    "op": "exists",
                }
            ),
        )

    def test_an_empty_fixtures_list_is_a_declaration(self):
        self.assertEqual(
            self._validate(
                fixtures=[],
                verification_spec=self._entry(
                    check={
                        "type": "resource_property",
                        "kind": "deployment",
                        "resource_name": "x",
                        "op": "exists",
                    }
                ),
            ),
            [],
        )

    def test_an_inline_verification_spec_is_rejected(self):
        # Loadable, and invisible to the presubmit's task_has_spec grep, so the
        # case runs its checks and is graded by the judge-only fallback anyway.
        self._only(
            "inline rather than as a block",
            text=(
                "id: made-up-case\n"
                "domain: security\n"
                "owner: maintainers\n"
                "verification_spec: [{name: n, role: objective, "
                "check: {type: report_contains, required_phrases: [x]}}]\n"
            ),
        )

    def test_an_entry_that_is_not_a_mapping_is_rejected(self):
        self._only("entry is not a mapping", verification_spec=["just-a-string"])

    def test_an_entry_with_no_name_is_rejected(self):
        self._only("entry has no 'name:'", verification_spec=self._entry(name=DELETE))

    def test_a_duplicate_entry_name_is_rejected(self):
        entry = self.VALID["verification_spec"][0]
        self._only("duplicate entry name", verification_spec=[entry, dict(entry)])

    def test_an_entry_with_no_check_is_rejected(self):
        self._only("entry has no 'check:' subtree", verification_spec=self._entry(check=DELETE))

    # -- the entry vocabulary devops-bench enforces at spec-load time --

    def test_an_entry_with_no_role_is_rejected(self):
        self._only("role None is not one of", verification_spec=self._entry(role=DELETE))

    def test_a_safeguard_with_no_severity_is_rejected(self):
        self._only(
            "a safeguard must declare a severity",
            verification_spec=self._entry(role="safeguard"),
        )

    def test_an_objective_with_a_severity_is_rejected(self):
        self._only(
            "on an objective",
            verification_spec=self._entry(severity="catastrophic"),
        )

    def test_an_unknown_severity_is_rejected(self):
        self._only(
            "is not one of ['catastrophic', 'recoverable']",
            verification_spec=self._entry(role="safeguard", severity="mild"),
        )

    def test_the_unbuilt_hold_mode_is_rejected(self):
        self._only("mode 'hold'", verification_spec=self._entry(mode="hold"))

    def test_a_non_positive_weight_is_rejected(self):
        self._only("must be a number greater than 0", verification_spec=self._entry(weight=0))

    # -- the check subtree --

    def test_a_check_with_no_assertion_is_rejected(self):
        self._only(
            "can only pass",
            verification_spec=self._entry(
                check={"type": "report_contains", "required_phrases": []}
            ),
        )

    def test_a_check_asserting_the_empty_string_is_rejected(self):
        # `"" in text` is true of every text there has ever been, so this is a
        # populated field that cannot fail -- the shape the rule exists for.
        self._only(
            "can only pass",
            verification_spec=self._entry(
                check={"type": "report_contains", "required_phrases": ["", "  "]}
            ),
        )

    def test_a_check_node_that_is_not_a_mapping_is_rejected(self):
        self._only("check node is not a mapping", verification_spec=self._entry(check="yes"))

    def test_a_check_with_no_type_is_rejected(self):
        self._only(
            "no 'type' discriminator",
            verification_spec=self._entry(check={"required_phrases": ["x"]}),
        )

    def test_an_unknown_check_type_is_rejected(self):
        self._only(
            "unknown check type",
            verification_spec=self._entry(check={"type": "wishful_thinking"}),
        )

    def test_an_empty_compound_check_is_rejected(self):
        self._only(
            "asserts nothing",
            verification_spec=self._entry(check={"type": "all", "checks": []}),
        )

    def test_a_compound_check_is_walked_to_its_leaves(self):
        self._only(
            "can only pass",
            verification_spec=self._entry(
                check={
                    "type": "all",
                    "checks": [
                        {"type": "report_contains", "required_phrases": ["x"]},
                        {"type": "report_contains", "required_phrases": []},
                    ],
                }
            ),
        )


class TestTheSanitizer(unittest.TestCase):
    """The fixture scan, against a tree built to trip exactly one rule at a time.

    Every credential here is assembled from a prefix and a run of one
    character, so it has the shape the redactor matches and nothing else:
    no real token's checksum, nothing a secret scanner would page anyone for.
    """

    def _findings(self, files, roots=None):
        """Findings for a temporary tree holding `files` ({relative path: text})."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content)
            found = validator.sanitization_findings(
                tuple(root / r for r in roots) if roots else (root,)
            )
            return [f.replace(tmp + "/", "") for f in found]

    def _only(self, needle, files):
        findings = self._findings(files)
        self.assertEqual(len(findings), 1, f"expected one finding, got {findings}")
        self.assertIn(needle, findings[0])
        return findings[0]

    def test_a_clean_tree_has_no_findings(self):
        self.assertEqual(self._findings({"case/task.yaml": "prompt: nothing to see\n"}), [])

    def test_each_rfc1918_range_is_rejected(self):
        for literal in ("10.1.2.3", "172.16.5.5", "172.31.255.1", "192.168.0.1"):
            with self.subTest(literal=literal):
                found = self._only("IPv4 literal", {"case/task.yaml": f"host: {literal}\n"})
                self.assertIn(literal, found)
                self.assertTrue(found.startswith("case/task.yaml:1:"), found)

    def test_a_public_literal_is_rejected(self):
        self._only("IPv4 literal 8.8.8.8", {"case/task.yaml": "dns: 8.8.8.8\n"})

    def test_an_address_ending_a_sentence_is_rejected(self):
        # Where an address sits in a prompt: followed by the full stop, not
        # by a fifth octet.
        for text in ("the node at 10.1.2.3.\n", "reach 10.1.2.3.\nThen stop.\n", "(10.1.2.3).\n"):
            with self.subTest(text=text):
                self._only("IPv4 literal 10.1.2.3", {"case/task.yaml": text})

    def test_leading_zero_octets_are_still_an_address(self):
        self._only("IPv4 literal 010.001.002.003", {"case/task.yaml": "host: 010.001.002.003\n"})

    def test_loopback_and_unspecified_take_the_marker(self):
        for literal in ("127.0.0.1", "0.0.0.0"):
            with self.subTest(literal=literal):
                self._only("IPv4 literal", {"case/x.yaml": f"bind: {literal}\n"})

    def test_each_documentation_range_passes(self):
        for literal in ("192.0.2.1", "198.51.100.200", "203.0.113.255"):
            with self.subTest(literal=literal):
                self.assertEqual(self._findings({"case/task.yaml": f"host: {literal}\n"}), [])

    def test_a_dotted_quad_that_is_not_an_address_passes(self):
        # A version tag, a five-part number and an octet over 255 are not
        # addresses, whatever they look like.
        text = "image: v1.2.3.4\nchain: 1.2.3.4.5\nbad: 300.1.1.1\n"
        self.assertEqual(self._findings({"case/task.yaml": text}), [])

    def test_each_credential_shape_is_rejected(self):
        shapes = {
            "a GCP API key": "AIza" + "0" * 35,
            "a GCP OAuth token": "ya29." + "a" * 20,
            "a GitHub token": "ghp_" + "a" * 20,
            "a GitHub fine-grained token": "github_pat_" + "a" * 20,
            "a Slack token": "xoxb-" + "0" * 10,
            "a JWT": "eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10,
            "an sk- API key": "sk-" + "a" * 20,
        }
        for label, value in shapes.items():
            with self.subTest(shape=label):
                self._only(label, {"case/task.yaml": f"value: {value}\n"})

    def test_a_private_key_block_is_rejected_at_its_first_line(self):
        pem = "-----BEGIN PRIVATE KEY-----\n" + "a" * 16 + "\n-----END PRIVATE KEY-----\n"
        found = self._only("a private-key block", {"case/key.pem": "# header\n" + pem})
        self.assertTrue(found.startswith("case/key.pem:2:"), found)

    def test_the_marker_with_a_reason_exempts_its_line(self):
        text = "bind: 127.0.0.1 # sanitizer: allow the kube-proxy default, not a host\n"
        self.assertEqual(self._findings({"case/task.yaml": text}), [])

    def test_the_marker_exempts_only_its_own_line(self):
        text = (
            "a: 127.0.0.1 # sanitizer: allow loopback\n"
            "b: 10.0.0.1\n"
        )
        found = self._only("IPv4 literal 10.0.0.1", {"case/task.yaml": text})
        self.assertTrue(found.startswith("case/task.yaml:2:"), found)

    def test_the_marker_on_the_first_line_exempts_a_key_block(self):
        pem = (
            "-----BEGIN PRIVATE KEY----- # sanitizer: allow a fixture key with no bits in it\n"
            + "a" * 16
            + "\n-----END PRIVATE KEY-----\n"
        )
        self.assertEqual(self._findings({"case/key.pem": pem}), [])

    def test_a_bare_marker_is_rejected(self):
        # Both the marker and the value it failed to exempt are reported: the
        # exemption is not applied without a reason.
        findings = self._findings({"case/task.yaml": "a: 10.0.0.1 # sanitizer: allow\n"})
        self.assertEqual(len(findings), 2, findings)
        self.assertIn("with no reason after it", findings[0])
        self.assertIn("IPv4 literal 10.0.0.1", findings[1])

    def test_a_bare_marker_on_a_crlf_line_is_still_bare(self):
        # The carriage return is not a reason.
        findings = self._findings({"case/task.yaml": "a: 10.0.0.1 # sanitizer: allow\r\nb: 1\r\n"})
        self.assertEqual(len(findings), 2, findings)
        self.assertIn("with no reason after it", findings[0])

    def test_a_bare_marker_on_a_clean_line_is_still_rejected(self):
        self._only("with no reason after it", {"case/task.yaml": "# sanitizer: allow\n"})

    def test_a_prebuilt_stack_file_is_scanned(self):
        found = self._only(
            "IPv4 literal 10.0.0.1",
            {
                "tasks/case/task.yaml": "ok: 192.0.2.1\n",
                "tf/prebuilt/stack/main.tf": 'ip = "10.0.0.1"\n',
            },
        )
        self.assertTrue(found.startswith("tf/prebuilt/stack/main.tf:1:"), found)

    def test_dot_directories_and_local_tofu_artefacts_are_skipped(self):
        files = {
            "stack/.terraform/x.txt": "10.0.0.1\n",
            "stack/.terraform.lock.hcl": "10.0.0.2\n",
            "stack/terraform.tfstate": "10.0.0.3\n",
            "stack/terraform.tfstate.backup": "10.0.0.4\n",
            "stack/terraform.tfvars": "10.0.0.5\n",
            "stack/prod.auto.tfvars": "10.0.0.6\n",
        }
        self.assertEqual(self._findings(files), [])

    def test_a_binary_file_is_skipped(self):
        self.assertEqual(self._findings({"case/blob.bin": b"x\0y 10.0.0.1"}), [])

    def test_only_the_named_roots_are_scanned(self):
        files = {"tasks/case/task.yaml": "a: 10.0.0.1\n", "elsewhere/x.txt": "b: 10.0.0.2\n"}
        findings = self._findings(files, roots=("tasks",))
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("10.0.0.1", findings[0])

    def test_the_shapes_are_the_redactors_own(self):
        # Imported, not copied: an extension of AuditRedactor reaches this
        # scan without a second edit, and a shape named here that the class
        # stops defining is a loud failure rather than a narrower scan.
        patterns = validator.credential_patterns()
        self.assertEqual(set(patterns), set(validator.CREDENTIAL_SHAPES.values()))
        with unittest.mock.patch.dict(
            validator.CREDENTIAL_SHAPES, {"NO_SUCH_PATTERN": "nothing"}, clear=False
        ):
            with self.assertRaises(validator.CaseError):
                validator.credential_patterns()

    def test_the_redactor_loads_even_though_it_defines_a_dataclass(self):
        # The loader runs the redactor outside the import system, which is
        # cheap until the file pairs a dataclass with `from __future__ import
        # annotations`. Every annotation is a string then, and dataclasses
        # probes an unqualified one for KW_ONLY through
        # sys.modules[cls.__module__] -- unguarded, so a module never
        # registered there dies on None, inside dataclasses and nowhere near
        # the shapes this scan wants. gke-labs/kube-agents#1364 added
        # RedactionRule and reded every test in this file. Both halves matter,
        # so both are here; a fixture of our own keeps this a test of the
        # loader after the redactor's contents move on.
        self.addCleanup(sys.modules.pop, validator.REDACTOR_MODULE_NAME, None)
        source = textwrap.dedent(
            '''
            from __future__ import annotations

            import re
            from dataclasses import dataclass

            @dataclass(frozen=True)
            class Rule:
                name: str

            class AuditRedactor:
                A_TOKEN = re.compile(r"tok-[0-9]+")
            '''
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "redactor.py"
            path.write_text(source, encoding="utf-8")
            with unittest.mock.patch.object(validator, "REDACTOR_FILE", path):
                with unittest.mock.patch.dict(
                    validator.CREDENTIAL_SHAPES, {"A_TOKEN": "a token"}, clear=True
                ):
                    self.assertEqual(
                        set(validator.credential_patterns()), {"a token"}
                    )
            # ..and it is still registered afterwards, the way the gateway's
            # loader in charts/kube-agents/files/litellm_redaction_callback.py
            # leaves it. Asserting the name is *absent* would be no test at
            # all -- that passes on the broken version too, which never
            # registers anything.
            loaded = sys.modules.get(validator.REDACTOR_MODULE_NAME)
            self.assertIsNotNone(loaded)
            self.assertEqual(pathlib.Path(loaded.__file__), path)

    def test_main_exits_non_zero_on_a_sanitization_finding(self):
        # main() scans the named case's directory, so a scratch draft is
        # checked with the fixtures beside it, and a finding reds the exit
        # code the way a rejected case does.
        spec = {**TestTheRulesReject.VALID}
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "made-up-case" / "task.yaml"
            path.parent.mkdir()
            path.write_text(yaml.safe_dump(spec))
            (path.parent / "values.yaml").write_text("upstream: 192.0.2.53\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(validator.main([str(path)]), 0)
            (path.parent / "values.yaml").write_text("upstream: 10.0.0.53\n")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(validator.main([str(path)]), 1)
            self.assertIn("unsanitized fixture", out.getvalue())
            self.assertIn("10.0.0.53", out.getvalue())


class TestTheAllowlistsAndTheSweep(unittest.TestCase):
    """The rules that run over the tree rather than over one file."""

    def test_a_stale_allowlist_entry_is_reported(self):
        # test_the_exclusion_lists_do_not_rot passes on a clean tree whether or
        # not this function does anything; this is what proves it does.
        with unittest.mock.patch.dict(
            validator.KNOWN_NO_DOMAIN, {"deleted-case": "gone"}, clear=False
        ):
            stale = validator.stale_allowlist_entries()
        self.assertIn("KNOWN_NO_DOMAIN: deleted-case", stale)

    def test_all_four_allowlists_are_swept(self):
        for name in ("KNOWN_UNREGISTERED", "KNOWN_NO_DOMAIN", "KNOWN_JUDGE_ONLY", "FIXTURE_NOT_READY"):
            with self.subTest(allowlist=name):
                with unittest.mock.patch.dict(
                    getattr(validator, name), {"deleted-case": "gone"}, clear=False
                ):
                    self.assertIn(f"{name}: deleted-case", validator.stale_allowlist_entries())

    def test_the_catch_all_fails_on_a_finding_no_per_rule_needle_matches(self):
        # TestEveryTaskIsValid.test_no_case_is_rejected_for_any_reason passes
        # on a clean tree whether or not it asserts anything, exactly like the
        # stale-allowlist sweep above; this is what proves it does. The planted
        # finding is deliberately one no per-rule needle in that class matches
        # -- the shape that was rejected by `make bench-case-check` and merged
        # green until the catch-all landed.
        case = TestEveryTaskIsValid("test_no_case_is_rejected_for_any_reason")
        with unittest.mock.patch.object(
            TestEveryTaskIsValid,
            "results",
            {"some-case": ["check 'x': duplicate entry name"]},
            create=True,
        ):
            with self.assertRaises(AssertionError) as raised:
                case.test_no_case_is_rejected_for_any_reason()
        self.assertIn("duplicate entry name", str(raised.exception))

    def test_the_sweep_reports_real_cases_not_a_parse_failure(self):
        # validate_all() returns a synthetic ROSTER_PARSE_KEY row when the
        # parse breaks, and every needle-based assertion in TestEveryTaskIsValid
        # would then pass over an empty corpus.
        results = validator.validate_all()
        self.assertNotIn(validator.ROSTER_PARSE_KEY, results)
        self.assertEqual(set(results), set(validator.bench_cases()))

    def test_a_fixture_not_ready_entry_that_a_roster_also_names_is_stale(self):
        with unittest.mock.patch.dict(
            validator.FIXTURE_NOT_READY, {"agent-kanban-smoke": "#1 waiting"}, clear=False
        ):
            self.assertIn(
                "FIXTURE_NOT_READY: agent-kanban-smoke (also in a roster file)",
                validator.stale_allowlist_entries(),
            )

    def test_a_fixture_not_ready_reason_without_an_issue_is_reported(self):
        with unittest.mock.patch.dict(
            validator.FIXTURE_NOT_READY, {"some-case": "waiting on a fixture"}, clear=False
        ):
            self.assertEqual(validator.fixture_not_ready_without_issue(), ["some-case"])

    def test_a_commented_out_registration_is_a_finding(self):
        # The retired parking state, planted in a scratch copy of the roster
        # files so the real tree stays untouched.
        with tempfile.TemporaryDirectory() as tmp:
            presubmit = pathlib.Path(tmp) / "presubmit-cases.txt"
            nightly = pathlib.Path(tmp) / "nightly-cases.txt"
            presubmit.write_text("./tasks/agent-kanban-smoke/task.yaml\n")
            nightly.write_text(
                "# blocked on #1\n# ./tasks/cluster-provision-kanban/task.yaml\n"
                "./tasks/obtainability-planted-pdb/task.yaml\n"
            )
            with unittest.mock.patch.object(validator, "ROSTER_FILES", (presubmit, nightly)):
                found = validator.commented_out_registrations()
                results = validator.validate_all()
        self.assertEqual(list(found), ["cluster-provision-kanban"])
        self.assertIn("is commented out in", " ".join(results["cluster-provision-kanban"]))
        self.assertIn("That parking state is retired", " ".join(results["cluster-provision-kanban"]))

    def test_one_unreadable_case_does_not_hide_the_others(self):
        broken = validator.TASKS_DIR / "zz-not-a-real-case" / "task.yaml"
        broken.parent.mkdir(parents=True)
        try:
            broken.write_text("id: [unclosed\n")
            results = validator.validate_all()
        finally:
            broken.unlink()
            broken.parent.rmdir()
        self.assertIn("could not be parsed as YAML", " ".join(results["zz-not-a-real-case"]))
        self.assertGreater(len(results), 1)


if __name__ == "__main__":
    unittest.main()
