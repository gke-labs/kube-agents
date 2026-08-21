"""Every bench task is either in the presubmit's TASKS array or excluded here.

`hack/ci-eval-pr.sh` runs the tasks listed in its TASKS array, and only those:
tasks under bench/tasks/ are not picked up automatically, the script's own
comment says so, and nothing owned the difference between "left out on
purpose" and "nobody remembered". That is how agent-kanban-smoke -- a task
whose whole point is to smoke the deployed pipeline -- sat registered nowhere
while the presubmit ran one task for months. A task nobody registered is the
same failure as a domain nobody covered.

This test owns that difference. A task passes by being named in TASKS (a
commented-out entry counts: it is registered, pending activation, which is how
scenarios wait for the seeded fleet) or by a reviewed entry in
KNOWN_UNREGISTERED with the reason. There is deliberately no nightly
exemption yet: no nightly runner exists anywhere in the tree, and an
exemption into a runner that does not exist is an exemption into nothing.
It returns with the nightly tier (testing implementation plan, Phase 4).

Today this lint and scripts/test_domain_coverage.py are independent: that
one counts a domain covered when any task carries its slug and a non-empty
verification_spec, without reading TASKS at all. The feat/domain-scenarios
branch redefines covered to require an ACTIVE, uncommented TASKS entry;
once that lands, the two lints ratchet together -- this one guarantees
every task is at least registered, that one guarantees a
registered-but-dormant task never counts as coverage.

TASKS is read from the script's text rather than by executing it: the script
provisions clusters and reads secrets, so running it to ask a question is not
an option. The parse is deliberately narrow -- the TASKS=( ... ) block only --
and a parse that finds nothing fails loudly rather than passing vacuously.
"""

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
TASKS_DIR = REPO_ROOT / "bench" / "tasks"

# Tasks that are neither in TASKS nor nightly-tiered, on purpose, for now.
# Every entry carries its reason; an entry without one should not survive
# review. Delete an entry once its task is registered -- staleness is only
# enforced for tasks that no longer exist, because an in-flight branch
# registering a task must not red main the day it merges.
KNOWN_UNREGISTERED = {
    # Registration lands with the exact-verifiers branch (the two-speed gate),
    # which also stops exporting BENCH_NO_INFRA so the task's tool_called
    # check actually runs. Until that merges, this entry is the record.
    "agent-kanban-smoke": "registered by the exact-verifiers branch, in flight",
    # Provisions its own cluster, so registering it costs every pull request
    # a second multi-minute provision. Whether it belongs in presubmit or
    # nightly is a tier decision nobody has made; this entry is the record
    # that the omission is known rather than accidental.
    "cluster-provision-kanban": "cluster-scoped provisioning task, tier decision pending",
}


def registered_tasks():
    """Task names in the TASKS array, commented entries included."""
    text = EVAL_SCRIPT.read_text()
    # Multi-line arrays end at a line holding only ")": stopping at the first
    # bare ")" character instead would truncate the block at a parenthesis
    # inside a comment and silently drop every entry below it.
    match = re.search(r"^TASKS=\((.*?)^\)", text, re.M | re.S) or re.search(
        r"^TASKS=\((.*)\)\s*$", text, re.M
    )
    if match is None:
        return None
    return set(re.findall(r"tasks/([A-Za-z0-9_-]+)/task\.yaml", match.group(1)))


def bench_tasks():
    return {p.parent.name: p for p in sorted(TASKS_DIR.glob("*/task.yaml"))}


class TestEveryTaskIsRegistered(unittest.TestCase):
    def test_every_task_is_in_tasks_or_nightly_or_excluded_by_name(self):
        registered = registered_tasks()
        self.assertIsNotNone(
            registered,
            "Could not find a TASKS=( ... ) array in hack/ci-eval-pr.sh -- "
            "the script changed shape and this test's parse needs updating.",
        )
        orphans = sorted(
            name
            for name in bench_tasks()
            if name not in registered and name not in KNOWN_UNREGISTERED
        )
        self.assertEqual(
            orphans,
            [],
            "\n\nThese bench tasks are registered nowhere and never run:\n  "
            + "\n  ".join(orphans)
            + "\n\nEither add each to TASKS in hack/ci-eval-pr.sh (a commented "
            "entry counts as registered, pending activation), or add it to "
            "KNOWN_UNREGISTERED in this file with the reason it must not run.",
        )

    def test_the_exclusion_list_does_not_rot(self):
        # An entry whose task directory is gone is stale noise. Entries whose
        # task has since been registered are pruned in review, not enforced
        # here -- see the comment above KNOWN_UNREGISTERED for why.
        existing = bench_tasks()
        stale = sorted(name for name in KNOWN_UNREGISTERED if name not in existing)
        self.assertEqual(
            stale,
            [],
            "\n\nThese KNOWN_UNREGISTERED entries match no bench task any "
            "more; delete them:\n  " + "\n  ".join(stale),
        )

    def test_the_parse_reads_a_nonempty_array(self):
        # If the TASKS parse ever comes back empty the first test would call
        # every task an orphan; fail with the real story instead.
        registered = registered_tasks()
        self.assertTrue(
            registered,
            "The TASKS array in hack/ci-eval-pr.sh parsed to no tasks -- "
            "either the array is empty or this test's parse has drifted.",
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


if __name__ == "__main__":
    unittest.main()
