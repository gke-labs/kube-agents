"""The fleet-shape probe's count lists are declared twice: the paths and the
Accept the event watcher sends (fleetShapeListPaths and fleetShapeAccept in
k8s-operator/cmd/k8s-event-watcher/fleetshape.go) and the paths and Accept
the credential broker admits (COUNT_LIST_PATHS and accepts_metadata_only in
agents/platform/scripts/watcher_policy.py). Each side's own tests pin its own
copy, so a kind added on one side with its test updated is green on both
suites and refused by the broker on every profile cluster at the next deploy.
This test fails when the two copies disagree.
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FLEETSHAPE_GO = REPO_ROOT / "k8s-operator/cmd/k8s-event-watcher/fleetshape.go"
POLICY_DIR = REPO_ROOT / "agents/platform/scripts"
# The table's opening and closing lines, and one entry of it, written
# `{path: "/api/v1/pods", pods: true},` or `{path: "/apis/apps/v1/deployments"},`:
# `path` first, a quoted literal, one line. An entry line in the block the
# regex does not read fails the parity test rather than dropping out of the
# comparison, so a reordered field, a wrapped entry or a named constant is a
# red, not a silent pass on the subset.
GO_LIST_PATHS_OPEN = "var fleetShapeListPaths = []fleetShapeListPath{"
GO_LIST_PATHS_CLOSE = "}"
GO_LIST_PATH = re.compile(r'^\s*\{path:\s*"([^"]+)"')
GO_COMMENT = "//"
GO_ACCEPT = re.compile(r'^\s*fleetShapeAccept\s*=\s*"([^"]+)"', re.MULTILINE)
GO_LIMIT = re.compile(r'^\s*fleetShapeListLimit\s*=\s*"([^"]+)"', re.MULTILINE)

sys.path.insert(0, str(POLICY_DIR))
import watcher_policy  # noqa: E402


def go_source() -> str:
    return FLEETSHAPE_GO.read_text(encoding="utf-8")


def go_list_paths(source: str) -> list[str]:
    """The paths of every entry of fleetShapeListPaths in source.

    Raises AssertionError when the table is missing, unclosed, or holds an
    entry line the entry regex does not read.
    """
    lines = source.splitlines()
    if GO_LIST_PATHS_OPEN not in lines:
        raise AssertionError(f"no line {GO_LIST_PATHS_OPEN!r} declares fleetShapeListPaths")
    start = lines.index(GO_LIST_PATHS_OPEN) + 1
    try:
        end = lines.index(GO_LIST_PATHS_CLOSE, start)
    except ValueError:
        raise AssertionError(f"fleetShapeListPaths opens at line {start} and never closes") from None
    entries = [line for line in lines[start:end] if line.strip() and not line.strip().startswith(GO_COMMENT)]
    unread = [line for line in entries if not GO_LIST_PATH.match(line)]
    if unread:
        raise AssertionError(
            "fleetShapeListPaths holds entries this test cannot read; write each as "
            '`{path: "<literal>", ...},` on one line, path first: ' + repr(unread)
        )
    return [GO_LIST_PATH.match(line).group(1) for line in entries]


class FleetShapeCountListParity(unittest.TestCase):
    def test_the_watcher_lists_exactly_the_paths_the_broker_counts(self) -> None:
        paths = go_list_paths(go_source())
        self.assertTrue(paths, f"{FLEETSHAPE_GO.relative_to(REPO_ROOT)} declares no fleetShapeListPaths entries")
        self.assertEqual(
            set(paths), {"/" + p for p in watcher_policy.COUNT_LIST_PATHS},
            "fleetShapeListPaths (Go) and COUNT_LIST_PATHS (Python) differ; the broker refuses "
            "what the watcher lists and every profile cluster reads as failed",
        )
        self.assertEqual(len(paths), len(set(paths)), f"fleetShapeListPaths repeats a path: {paths}")

    def test_an_entry_the_regex_cannot_read_fails_rather_than_drops(self) -> None:
        """A sixth Go entry spelled in a way the entry regex does not read is a
        red, not a five-path set that happens to equal the broker's."""
        table = "\n".join(
            [GO_LIST_PATHS_OPEN]
            + [f'\t{{path: "/{p}"}},' for p in sorted(watcher_policy.COUNT_LIST_PATHS)]
            + ["\t{EXTRA}", GO_LIST_PATHS_CLOSE]
        )
        for extra in (
            '{pods: false, path: "/apis/batch/v1/jobs"},',
            '{path: jobsPath},',
            'path: "/apis/batch/v1/jobs",\n\t},',
        ):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(AssertionError, "cannot read"):
                    go_list_paths(table.replace("{EXTRA}", extra))
        self.assertEqual(
            sorted(go_list_paths(table.replace("\t{EXTRA}\n", ""))),
            sorted("/" + p for p in watcher_policy.COUNT_LIST_PATHS),
        )
        with self.assertRaisesRegex(AssertionError, "never closes"):
            go_list_paths(table.replace("{EXTRA}", "").rsplit("\n", 1)[0])

    def test_the_accept_the_watcher_sends_is_one_the_broker_admits(self) -> None:
        found = GO_ACCEPT.search(go_source())
        self.assertIsNotNone(found, f"{FLEETSHAPE_GO.relative_to(REPO_ROOT)} declares no fleetShapeAccept")
        accept = found.group(1)
        self.assertTrue(
            watcher_policy.accepts_metadata_only(accept),
            f"the watcher's Accept {accept!r} is not one the broker's count-list rule admits",
        )
        decision = watcher_policy.evaluate_k8s(
            "GET", "kagents-dev/us-east4/fleet-a/" + sorted(watcher_policy.COUNT_LIST_PATHS)[0],
            {"limit": ["1"]}, accept,
        )
        self.assertEqual(decision.rule_id, watcher_policy.RULE_K8S_COUNT_LIST)

    def test_the_limit_the_watcher_sends_is_the_query_the_broker_admits(self) -> None:
        found = GO_LIMIT.search(go_source())
        self.assertIsNotNone(found, f"{FLEETSHAPE_GO.relative_to(REPO_ROOT)} declares no fleetShapeListLimit")
        self.assertEqual({"limit": [found.group(1)]}, watcher_policy.COUNT_LIST_QUERY)


if __name__ == "__main__":
    unittest.main()
