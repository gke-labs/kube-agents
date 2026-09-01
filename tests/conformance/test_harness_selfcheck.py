"""Could the harness itself be the thing that passes?

Slice 2a shipped a whole gate that could be deleted with its test suite
byte-identical. Slice 2b's task 5 reported mutation results from a stub harness
and part of the transcript turned out to be fiction -- the harness injected a
return code at a layer that could not model the behaviour it claimed to test.
Both times the question that would have caught it was asked late or by
accident.

So it is asked here, first, every run. Everything in this module is about the
suite rather than about the product:

  * every artifact a test reads exists, is non-empty, and still contains the
    anchor that makes reading it meaningful
  * the modules the suite imports are the ones that ship
  * every expected failure is registered with an invariant and a reference
  * the bucket-2 scenarios are skipped for the stated reason and not because
    they failed to load
"""

from __future__ import annotations

import importlib
import pkgutil
import unittest
from pathlib import Path

from . import _harness as h

_INVARIANTS = {
    "A1", "A2", "A3", "A4",
    "B1", "B2", "B3", "B4", "B5", "B6",
    "C1", "C2", "C3", "C4", "C5",
    "D1", "D2", "D3", "D4", "D5", "D6",
}

_TEST_MODULES = (
    "test_A_authority",
    "test_B_write_path",
    "test_C_enforcement",
    "test_D_accountability",
    "test_D15_parser_differentials",
)


def _load_every_test_module():
    """Import all of them, so KNOWN_VIOLATIONS is fully populated.

    Discovery order is alphabetical and this module sorts last, but relying on
    that would make the registry assertions below depend on a filename.
    """
    for name in _TEST_MODULES:
        importlib.import_module(f".{name}", package=__package__)


class TheSourcesAreReal(unittest.TestCase):
    """The registry is the suite's contract with the repository."""

    def test_every_registered_source_exists_and_is_readable(self) -> None:
        for name in sorted(h.SOURCES):
            with self.subTest(source=name):
                path = h.path_of(name)
                self.assertTrue(path.is_file(), f"{path} does not exist")
                self.assertTrue(h.text(name).strip(), f"{path} is empty")

    def test_every_anchor_is_still_present(self) -> None:
        """An anchor is the substring whose loss makes a test meaningless.

        A conformance test that greps a renamed symbol does not fail -- it
        finds nothing and asserts nothing, or it raises inside an expected
        failure and is counted as a pass. This is the one place that turns a
        rename into a red test.
        """
        for name, source in sorted(h.SOURCES.items()):
            self.assertTrue(
                source.anchors,
                f"{name} is registered with no anchor, so nothing detects it "
                f"being gutted",
            )
            body = h.text(name)
            for anchor in source.anchors:
                with self.subTest(source=name, anchor=anchor):
                    self.assertIn(anchor, body, f"{source.path} no longer contains it")

    def test_the_imported_policy_modules_are_the_ones_that_ship(self) -> None:
        """Guards against a same-named module elsewhere on sys.path.

        `command_policy` and `credential_proxy` are plain scripts rather than a
        package, so they are reached by prepending a directory to sys.path.
        That is exactly the mechanism by which a stale copy in a virtualenv or
        a sibling checkout would be imported instead, and every argv assertion
        in the suite would then be about the wrong file.
        """
        self.assertEqual(
            h.path_of("command_policy"), Path(h.command_policy.__file__).resolve()
        )
        self.assertEqual(
            h.path_of("credential_proxy"), Path(h.credential_proxy.__file__).resolve()
        )

    def test_the_golden_fixtures_render_more_than_a_stub(self) -> None:
        """Four fixtures, each a full object set.

        Several assertions iterate the fixtures and would pass vacuously over
        an empty parse -- a YAML change that made `safe_load_all` yield nothing
        would turn most of group C green rather than red.
        """
        for name, documents in h.golden_documents().items():
            with self.subTest(fixture=name):
                self.assertGreaterEqual(len(documents), 8, "fixture parsed too thin")
                kinds = {d.get("kind") for d in documents}
                self.assertIn("Deployment", kinds)
                self.assertIn("ClusterRole", kinds)


class TheExpectedFailuresAreDeclared(unittest.TestCase):
    """An expected failure is a finding, so it carries a finding's metadata."""

    def setUp(self) -> None:
        _load_every_test_module()

    def test_every_known_violation_names_an_invariant_and_a_reference(self) -> None:
        self.assertTrue(
            h.KNOWN_VIOLATIONS,
            "no known violations are registered; either the product became "
            "conformant or the decorator stopped recording",
        )
        for test, (invariant, reference) in sorted(h.KNOWN_VIOLATIONS.items()):
            with self.subTest(test=test):
                self.assertIn(
                    invariant,
                    _INVARIANTS,
                    f"{invariant!r} is not one of the 21 invariants",
                )
                self.assertRegex(
                    reference,
                    r"\.md\b",
                    "a known violation must cite the document it is recorded in",
                )

    def test_every_known_violation_has_a_precondition_test(self) -> None:
        """The hole an expected failure leaves, and how it is plugged.

        `unittest.expectedFailure` swallows every exception, including the
        `FileNotFoundError` raised when the artifact under test has moved. So
        each violation test is paired with a plainly-passing precondition that
        asserts the artifact and its anchor are still there. Without the pair,
        a rename turns a recorded finding into a silent pass.

        Enforced structurally rather than by convention: the pairing is what
        makes the suite honest about the things it says are broken, which are
        the assertions a reviewer will look at hardest.
        """
        for qualname in sorted(h.KNOWN_VIOLATIONS):
            class_name, _, _ = qualname.partition(".")
            with self.subTest(test=qualname):
                found = False
                for module_name in _TEST_MODULES:
                    module = importlib.import_module(
                        f".{module_name}", package=__package__
                    )
                    owner = getattr(module, class_name, None)
                    if owner is None:
                        continue
                    found = any(
                        name.startswith("test_") and "precondition" in name
                        for name in dir(owner)
                    )
                    if found:
                        break
                self.assertTrue(
                    found,
                    f"{class_name} registers a known violation but declares no "
                    f"precondition test, so a rename would pass silently",
                )


class TheBucketTwoScenariosLoad(unittest.TestCase):
    """A skipped test and a broken test look identical in a summary line."""

    def test_the_cluster_scenarios_import_without_a_cluster(self) -> None:
        module = importlib.import_module(".bucket2.test_cluster_scenarios", package=__package__)
        scenarios = [
            name
            for name in dir(module)
            if name.startswith("Scenario") or name.endswith("Scenarios")
        ]
        self.assertTrue(scenarios, "bucket 2 declares no scenarios")

    def test_bucket_two_is_skipped_for_the_stated_reason(self) -> None:
        """Not skipped because it raised on import, and not silently running.

        The scenarios mutate a cluster. Gating them on an explicit environment
        variable rather than on the presence of a kubeconfig is what stops them
        running against whatever cluster a developer was last pointed at.
        """
        import os

        loader = unittest.TestLoader()
        suite = loader.discover(
            str(h.REPO_ROOT / "tests" / "conformance" / "bucket2"),
            pattern="test_*.py",
            top_level_dir=str(h.REPO_ROOT / "tests"),
        )
        self.assertEqual([], loader.errors, "bucket 2 failed to load")

        count = suite.countTestCases()
        self.assertGreater(count, 0, "bucket 2 discovered no tests")

        if os.environ.get("KUBE_AGENTS_CONFORMANCE_CLUSTER"):
            self.skipTest("a cluster is configured; the scenarios are running for real")
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(count, len(result.skipped), "a bucket-2 scenario ran anyway")
        self.assertEqual([], result.errors)
        self.assertEqual([], result.failures)


class TheSuiteCoversEveryInvariant(unittest.TestCase):
    """The exit criterion, checked by the suite rather than by the README."""

    def test_every_invariant_is_named_by_at_least_one_test_or_reason(self) -> None:
        """A test per invariant, or an explicit written reason it is bucket 3.

        Both count, and both have to be *in the code*: a bucket-3 invariant is
        covered by a class whose docstring says BUCKET 3 and why. What does not
        count is an invariant that appears nowhere, which is how a set of
        twenty-one becomes a suite about the eight that were easy.
        """
        named: dict[str, set[str]] = {invariant: set() for invariant in _INVARIANTS}
        for module_name in _TEST_MODULES:
            module = importlib.import_module(f".{module_name}", package=__package__)
            for attribute in dir(module):
                value = getattr(module, attribute)
                if not isinstance(value, type) or not issubclass(value, unittest.TestCase):
                    continue
                for name in dir(value):
                    if not name.startswith("test_"):
                        continue
                    for invariant in _INVARIANTS:
                        if name.startswith(f"test_{invariant}_"):
                            named[invariant].add(f"{module_name}.{value.__name__}.{name}")
                for invariant in _INVARIANTS:
                    if invariant in value.__name__ and "BUCKET 3" in (value.__doc__ or ""):
                        named[invariant].add(f"{module_name}.{value.__name__} (bucket 3)")

        uncovered = sorted(name for name, tests in named.items() if not tests)
        self.assertEqual(
            [],
            uncovered,
            f"{uncovered} appear nowhere in the suite -- neither as a test nor "
            f"as a written bucket-3 reason",
        )

    def test_no_test_module_is_left_out_of_the_self_check(self) -> None:
        """The list above is the thing that would drift.

        Adding a test module and forgetting to register it here means its
        known violations and its invariant coverage are invisible to every
        assertion in this file.
        """
        package_dir = Path(__file__).parent
        discovered = {
            name
            for _, name, _ in pkgutil.iter_modules([str(package_dir)])
            if name.startswith("test_") and name != "test_harness_selfcheck"
        }
        self.assertEqual(set(_TEST_MODULES), discovered)


class EveryAssertionHasBeenAttacked(unittest.TestCase):
    """A test nobody has tried to break is a test whose existence is a guess.

    `hack/conformance-mutations.py` is what breaks them, and running it is
    manual -- it edits tracked files in place, so it cannot go in CI. That
    makes its coverage the thing that rots, and it had: 43 mutations naming 42
    distinct tests while 29 passing assertions were named by none, under a
    README saying every test had been verified. The claim was in prose, and
    prose does not fail.

    This is the mechanism. It reads the mutation set as data and requires a
    mutation per assertion, which is cheap because a new test that nobody
    mutated fails here on the same pull request that adds it.

    Three limits, stated rather than discovered later:

    It cannot check that the mutation is a *good* one -- that it removes the
    control rather than something adjacent to it. Two of the original 43 named
    a control the test did not actually assert, and only the run itself caught
    them. So this is a floor, not a proof.

    It skips precondition tests as well as the two exemptions below. A
    precondition asserts that an artifact and its anchor are still present, and
    the mechanism behind all of them is mutated once, by harness-source-moved,
    rather than once per test.

    It walks _TEST_MODULES, which does not include this module, so the
    self-check does not police its own coverage. That is a real gap and not a
    clean one to close: several assertions here have no string-replace that
    removes their control -- falsifying "no test module is left out" means
    adding a file, not editing one. The four harness-* mutations cover four of
    them; the rest are not covered.
    """

    #: Assertions with no in-repo control to remove. Each needs a reason, and
    #: the reason has to be that mutating it would edit the assertion rather
    #: than the thing asserted -- not that a mutation was hard to think of.
    _NO_CONTROL_TO_REMOVE = {
        "test_D15_the_mapped_prefix_still_denotes_the_whole_ipv4_internet": (
            "asserts a property of the ipaddress module: that ::ffff:0.0.0.0/96 "
            "unmaps to 0.0.0.0/0 and contains the metadata address. It holds the "
            "premise the Go guard rests on as an executable statement rather "
            "than a comment, and reads no repository artifact, so any edit that "
            "reddens it is an edit to the assertion. The controls the premise "
            "underwrites are mutated: D15-guard-normalises and C1-cidr-guard-inert."
        ),
    }

    @staticmethod
    def _mutation_targets() -> list[str]:
        """The `kills` field of every mutation, read without importing it.

        The harness is a script with a hyphen in its name, in a directory that
        is not a package, so there is no import statement that reaches it and
        loading it by path to read one list is more machinery than reading the
        literal out of the AST. Which also means a syntax error there fails
        here.
        """
        import ast

        source = Path(__file__).resolve().parents[2] / "hack" / "conformance-mutations.py"
        targets = []
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "Mutation":
                continue
            if len(node.args) >= 4 and isinstance(node.args[3], ast.Constant):
                targets.append(node.args[3].value)
        return targets

    def test_every_bucket_one_assertion_is_named_by_a_mutation(self) -> None:
        _load_every_test_module()
        targets = self._mutation_targets()
        self.assertGreater(len(targets), 60, "the mutation set failed to parse")

        unattacked = []
        for module_name in _TEST_MODULES:
            module = importlib.import_module(f".{module_name}", package=__package__)
            for attribute in sorted(dir(module)):
                value = getattr(module, attribute)
                if not isinstance(value, type) or not issubclass(value, unittest.TestCase):
                    continue
                for name in sorted(dir(value)):
                    if not name.startswith("test_") or "_precondition_" in name:
                        # A precondition test is the pair of a known violation
                        # and asserts an artifact is still there; harness-source-moved
                        # is the mutation that covers the whole mechanism.
                        continue
                    if f"{value.__name__}.{name}" in h.KNOWN_VIOLATIONS:
                        # A known violation asserts a control that does not
                        # exist, so there is nothing to delete. It is verified
                        # in the other direction: its precondition test above,
                        # which test_every_known_violation_has_a_precondition_test
                        # requires, plus the unexpected success it produces the
                        # day the control lands.
                        continue
                    if name in self._NO_CONTROL_TO_REMOVE:
                        continue
                    if not any(target in name for target in targets):
                        unattacked.append(f"{module_name}.{value.__name__}.{name}")

        self.assertEqual(
            [],
            sorted(set(unattacked)),
            "no mutation in hack/conformance-mutations.py names these, so "
            "nothing has ever confirmed they fail when their control is "
            "removed. Add a mutation, run it, and record the verdict -- or add "
            "the test to _NO_CONTROL_TO_REMOVE with a reason",
        )

    def test_the_exemptions_are_argued_rather_than_listed(self) -> None:
        """An exemption list with no reasons is how the floor gets lowered."""
        for name, reason in self._NO_CONTROL_TO_REMOVE.items():
            self.assertTrue(name.startswith("test_"), name)
            self.assertGreater(
                len(reason), 120, f"{name}'s exemption needs an argument, not a note"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
