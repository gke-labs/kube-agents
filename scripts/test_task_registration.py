"""Every bench case is valid, and either in the presubmit's TASKS array or excluded.

`hack/ci-eval-pr.sh` runs the tasks listed in its TASKS array, and only those:
tasks under bench/tasks/ are not picked up automatically, the script's own
comment says so, and nothing owned the difference between "left out on
purpose" and "nobody remembered". That is how agent-kanban-smoke -- a task
whose whole point is to smoke the deployed pipeline -- sat registered nowhere
while the presubmit ran one task for months. A task nobody registered is the
same failure as a domain nobody covered.

This test owns that difference, and four more like it. The rules and the
allowlists live in scripts/validate_bench_cases.py, which `make
bench-case-check` also runs. Sharing the implementation is only half of what
it takes for the two to agree: this lint also has to assert on everything that
module returns rather than on a hand-listed set of substrings, which is what
TestEveryTaskIsValid's whole-set assertion is for -- see its docstring for the
rules that leaked through before it existed. `make bench-case-check` is
invoked by no workflow; this lint, reached through PYTHON_TEST_DIRS
(Makefile:129) and run by .github/workflows/python-tests.yml, is the whole of
the enforcement on a pull request. A case passes by being named in
TASKS (a commented-out entry counts: it is registered, pending activation,
which is how scenarios wait for the seeded fleet) or by a reviewed entry in
the validator's KNOWN_UNREGISTERED with the reason. There is deliberately no
nightly exemption yet: no nightly runner exists anywhere in the tree, and an
exemption into a runner that does not exist is an exemption into nothing.
It returns with the nightly tier (testing implementation plan, Phase 4).

This lint and scripts/test_domain_coverage.py ratchet together. That one
counts a domain covered only when a task carrying its slug and a non-empty
verification_spec has an ACTIVE, uncommented TASKS entry, so a
registered-but-dormant task never counts as coverage; this one guarantees
every task is at least registered and internally valid.

TASKS is read from the script's text rather than by executing it: the script
provisions clusters and reads secrets, so running it to ask a question is not
an option. The parse is deliberately narrow -- the TASKS=( ... ) block only --
and a parse that finds nothing fails loudly rather than passing vacuously.
"""

import contextlib
import io
import pathlib
import re
import sys
import tempfile
import unittest
import unittest.mock

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import validate_bench_cases as validator  # noqa: E402

# Sentinel for "this case omits the key entirely", which None cannot express:
# `domain: null` and no `domain:` line are the same thing to yaml.safe_load,
# but a test needs to say which one it means.
DELETE = object()

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"


class TestEveryTaskIsRegistered(unittest.TestCase):
    def test_every_task_is_in_tasks_or_nightly_or_excluded_by_name(self):
        registered = validator.registered_cases()
        self.assertIsNotNone(
            registered,
            "Could not find a TASKS=( ... ) array in hack/ci-eval-pr.sh -- "
            "the script changed shape and the validator's parse needs updating.",
        )
        orphans = sorted(
            name
            for name in validator.bench_cases()
            if name not in registered and name not in validator.KNOWN_UNREGISTERED
        )
        self.assertEqual(
            orphans,
            [],
            "\n\nThese bench tasks are registered nowhere and never run:\n  "
            + "\n  ".join(orphans)
            + "\n\nEither add each to TASKS in hack/ci-eval-pr.sh (a commented "
            "entry counts as registered, pending activation), or add it to "
            "KNOWN_UNREGISTERED in scripts/validate_bench_cases.py with the "
            "reason it must not run.",
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

    def test_the_parse_reads_a_nonempty_array(self):
        # If the TASKS parse ever comes back empty the first test would call
        # every task an orphan; fail with the real story instead.
        self.assertTrue(
            validator.registered_cases(),
            "The TASKS array in hack/ci-eval-pr.sh parsed to no tasks -- "
            "either the array is empty or the validator's parse has drifted.",
        )

    def test_the_array_is_declared_exactly_once(self):
        # The parse reads the first TASKS=( ... ) block. A later reassignment
        # would silently win in the shell while this test kept reading the
        # first -- the one escape that would not fail loudly red.
        # Anchored to line start: FAILED_TASKS=( contains the bare substring.
        count = len(re.findall(r"^TASKS=\(", EVAL_SCRIPT.read_text(), re.M))
        self.assertEqual(
            count,
            1,
            f"hack/ci-eval-pr.sh declares TASKS=( {count} times; this test "
            "reads the first, the shell obeys the last -- keep it to one.",
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
            "what TASKS, the results file and every lint key on:",
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

    def test_all_three_allowlists_are_swept(self):
        for name in ("KNOWN_UNREGISTERED", "KNOWN_NO_DOMAIN", "KNOWN_JUDGE_ONLY"):
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
        # validate_all() returns a synthetic "<TASKS array>" row when the parse
        # breaks, and every needle-based assertion in TestEveryTaskIsValid
        # would then pass over an empty corpus.
        results = validator.validate_all()
        self.assertNotIn("<TASKS array>", results)
        self.assertEqual(set(results), set(validator.bench_cases()))

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
