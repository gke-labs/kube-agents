"""The fleet-shape probe's count lists are declared twice: the paths and the
Accept the event watcher sends (fleetShapeListPaths and fleetShapeAccept in
k8s-operator/cmd/k8s-event-watcher/fleetshape.go) and the paths and Accept
the credential broker admits (COUNT_LIST_PATHS and accepts_metadata_only in
agents/platform/scripts/watcher_policy.py). Each side's own tests pin its own
copy, so a kind added on one side with its test updated is green on both
suites and refused by the broker on every profile cluster at the next deploy.
This test fails when the two copies disagree. The node count is a third pair:
the GKE field the Go side reads off the describe and the fields the broker's
cluster projection keeps; a field dropped from the projection reads 0 with the
cluster still counted, so that pair is pinned here too.
"""

from __future__ import annotations

import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FLEETSHAPE_GO = REPO_ROOT / "k8s-operator/cmd/k8s-event-watcher/fleetshape.go"
POLICY_DIR = REPO_ROOT / "agents/platform/scripts"
# The table's opening and closing lines. The block between them is read as a
# whole, not line by line: every struct literal in it, however many share a
# line and however an entry wraps, is parsed into its fields, and anything in
# the block that is not such a literal (a named constant, a variable, a field
# this test does not know) fails the parity test rather than dropping out of
# the comparison.
GO_LIST_PATHS_OPEN = "var fleetShapeListPaths = []fleetShapeListPath{"
GO_LIST_PATHS_CLOSE = "}"
GO_COMMENT = re.compile(r"//.*$", re.MULTILINE)
# One struct literal and its trailing comma; the fields inside are split on
# commas, which no field value here carries.
GO_ENTRY = re.compile(r"\s*\{([^{}]*)\}\s*,?")
GO_FIELD = re.compile(r"^(\w+)\s*:\s*(.*)$", re.DOTALL)
GO_STRING = re.compile(r'^"([^"\\]*)"$')
GO_PATH_FIELD = "path"
# The other field an entry may carry, and the values it may take.
GO_BOOL_FIELDS = {"pods": {"true", "false"}}
GO_ACCEPT = re.compile(r'^\s*fleetShapeAccept\s*=\s*"([^"]+)"', re.MULTILINE)
GO_LIMIT = re.compile(r'^\s*fleetShapeListLimit\s*=\s*"([^"]+)"', re.MULTILINE)
# The fields the probe reads off the GKE describe (`described.<Field>` in
# measureCluster). google.golang.org/api names a JSON field by its Go field
# with the first letter lowered, which is how each is looked up in the
# broker's CLUSTER_FIELDS.
GO_DESCRIBED_FIELD = re.compile(r"\bdescribed\.(\w+)")

sys.path.insert(0, str(POLICY_DIR))
import watcher_policy  # noqa: E402


def go_source() -> str:
    return FLEETSHAPE_GO.read_text(encoding="utf-8")


def go_list_paths(source: str) -> list[str]:
    """The paths of every entry of fleetShapeListPaths in source.

    Raises AssertionError when the table is missing or unclosed, or when the
    block holds anything but struct literals of a quoted `path` and, at most,
    a boolean `pods`.
    """
    lines = source.splitlines()
    if GO_LIST_PATHS_OPEN not in lines:
        raise AssertionError(f"no line {GO_LIST_PATHS_OPEN!r} declares fleetShapeListPaths")
    start = lines.index(GO_LIST_PATHS_OPEN) + 1
    try:
        end = lines.index(GO_LIST_PATHS_CLOSE, start)
    except ValueError:
        raise AssertionError(f"fleetShapeListPaths opens at line {start} and never closes") from None
    block = GO_COMMENT.sub("", "\n".join(lines[start:end]))
    paths: list[str] = []
    position = 0
    while position < len(block):
        if not block[position:].strip():
            break
        entry = GO_ENTRY.match(block, position)
        if entry is None:
            raise AssertionError(
                "fleetShapeListPaths holds something this test cannot read; write each entry as "
                '`{path: "<literal>", ...},`: ' + repr(block[position:].strip())
            )
        paths.append(go_entry_path(entry.group(1)))
        position = entry.end()
    return paths


def go_entry_path(fields: str) -> str:
    """The quoted path of one struct literal's fields, or AssertionError."""
    path = None
    for field in filter(None, (f.strip() for f in fields.split(","))):
        parsed = GO_FIELD.match(field)
        if parsed is None:
            raise AssertionError(f"fleetShapeListPaths entry field this test cannot read: {field!r}")
        name, value = parsed.group(1), parsed.group(2).strip()
        if name == GO_PATH_FIELD:
            literal = GO_STRING.match(value)
            if literal is None or path is not None:
                raise AssertionError(f"fleetShapeListPaths entry path is not one quoted literal: {field!r}")
            path = literal.group(1)
        elif name not in GO_BOOL_FIELDS or value not in GO_BOOL_FIELDS[name]:
            raise AssertionError(f"fleetShapeListPaths entry field this test cannot read: {field!r}")
    if path is None:
        raise AssertionError(f"fleetShapeListPaths entry without a path: {fields!r}")
    return path


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

    def test_an_entry_the_parser_cannot_read_fails_rather_than_drops(self) -> None:
        """Every struct literal in the block is read, two to a line or wrapped
        over several and whichever field first; anything else in the block is
        a red, not a five-path set that happens to equal the broker's."""
        five = sorted("/" + p for p in watcher_policy.COUNT_LIST_PATHS)
        table = "\n".join([GO_LIST_PATHS_OPEN] + [f'\t{{path: "{p}"}},' for p in five] + ["\t{EXTRA}", GO_LIST_PATHS_CLOSE])
        for extra in (
            '{path: "/apis/batch/v1/jobs"}, {path: "/apis/batch/v1/jobs2"},',
            '{pods: false, path: "/apis/batch/v1/jobs"},',
            '{\n\t\tpath: "/apis/batch/v1/jobs",\n\t\tpods: true,\n\t},',
            '{path: "/apis/batch/v1/jobs"}, // a trailing comment\n\t// a comment line\n',
        ):
            with self.subTest(extra=extra):
                self.assertIn("/apis/batch/v1/jobs", go_list_paths(table.replace("{EXTRA}", extra)))
        self.assertEqual(
            go_list_paths(table.replace("{EXTRA}", '{path: "/a"}, {path: "/b"},')), five + ["/a", "/b"],
        )
        for extra in (
            "{path: jobsPath},",
            "jobsEntry,",
            '{path: "/apis/batch/v1/jobs", limit: 2},',
            '{path: "/apis/batch/v1/jobs", pods: maybe},',
            '{pods: true},',
            '{path: "/a", path: "/b"},',
            '{path: "/apis/batch/v1/jobs" + suffix},',
        ):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(AssertionError, "cannot read|not one quoted literal|without a path"):
                    go_list_paths(table.replace("{EXTRA}", extra))
        self.assertEqual(sorted(go_list_paths(table.replace("\t{EXTRA}\n", ""))), five)
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

    def test_the_node_count_the_watcher_reads_is_a_field_the_broker_relays(self) -> None:
        """The node count rides on the broker's cluster projection keeping the
        field the probe reads off the describe: dropped there, every profile
        cluster reads 0 nodes with outcome counted, and nothing else says so."""
        fields = set(GO_DESCRIBED_FIELD.findall(go_source()))
        self.assertTrue(fields, f"{FLEETSHAPE_GO.relative_to(REPO_ROOT)} reads no field off the describe")
        for field in sorted(fields):
            json_name = field[0].lower() + field[1:]
            self.assertIn(
                json_name, watcher_policy.CLUSTER_FIELDS,
                f"the probe reads described.{field} but the broker's projection drops {json_name!r}; "
                "through the broker that cluster would read 0 nodes and still count",
            )
        self.assertIn("CurrentNodeCount", fields)


if __name__ == "__main__":
    unittest.main()
