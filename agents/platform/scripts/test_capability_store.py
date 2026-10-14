"""Unit tests for capability_store: the runtime-owned criteria behind the delivery vehicle.

Run: python3 -m unittest agents.platform.scripts.test_capability_store
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capability_store as cs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED = REPO_ROOT / "agents" / "platform" / "capabilities"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "threshold": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3},
        "names": {"type": "array", "items": {"type": "string"}, "maxItems": 3, "default": []},
        "mode": {"type": "string", "enum": ["quiet", "loud"], "default": "quiet"},
    },
}


def seed(root: Path, name: str = "demo", criteria=None, learning=None, schema=SCHEMA) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / cs.CRITERIA_FILENAME).write_text(json.dumps(criteria if criteria is not None else {"threshold": 3}))
    (d / cs.SCHEMA_FILENAME).write_text(json.dumps(schema))
    # The policy file is always there, as the template ships it; a capability without one is refused.
    (d / cs.LEARNING_FILENAME).write_text(json.dumps(learning if learning is not None else {}))
    return d


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_get_merges_schema_defaults_under_stored_values(self):
        seed(self.root, criteria={"threshold": 5})
        got = cs.describe(cs.load(self.root, "demo"))
        self.assertEqual(got["criteria"], {"threshold": 5, "names": [], "mode": "quiet"})
        self.assertEqual(got["revision"], 0)
        self.assertEqual(got["keys"]["threshold"]["policy"], cs.POLICY_PROPOSE)
        self.assertEqual(got["stale_keys"], [])

    def test_a_propose_key_needs_a_confirmer(self):
        seed(self.root)
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="too noisy")
        self.assertIn("propose", str(ctx.exception))
        # Nothing was written: no revision, no changelog.
        self.assertEqual(cs.load(self.root, "demo").criteria, {"threshold": 3})
        self.assertFalse((self.root / "demo" / cs.CHANGELOG_FILENAME).exists())

    def test_a_confirmed_change_is_written_atomically_with_a_changelog_line(self):
        seed(self.root)
        entry = cs.apply_changes(
            self.root, "demo", {"threshold": 4}, reason="too noisy", confirmed_by="ops@example"
        )
        cap = cs.load(self.root, "demo")
        self.assertEqual(cap.criteria["threshold"], 4)
        self.assertEqual(cap.criteria[cs.REVISION_KEY], 1)
        self.assertIn(cs.UPDATED_AT_KEY, cap.criteria)
        self.assertEqual(entry["changes"], {"threshold": {"before": 3, "after": 4}})
        self.assertEqual(entry["mode"], cs.MODE_CONFIRMED)
        self.assertEqual(entry["pruned"], {})
        self.assertEqual(cs.history(self.root, "demo"), [entry])
        self.assertFalse(list((self.root / "demo").glob("*.tmp")))

    def test_an_autonomous_key_needs_only_a_reason(self):
        seed(self.root, learning={"default": "propose", "keys": {"names": "autonomous"}})
        entry = cs.apply_changes(self.root, "demo", {"names": ["a"]}, reason="operator asked twice")
        self.assertEqual(entry["mode"], cs.MODE_AUTONOMOUS)
        self.assertEqual(cs.load(self.root, "demo").criteria["names"], ["a"])

    def test_a_never_key_is_refused_even_when_confirmed(self):
        seed(self.root, criteria={"threshold": 6}, learning={"keys": {"threshold": "never"}})
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"threshold": 9}, reason="x", confirmed_by="ops")
        self.assertIn("never", str(ctx.exception))
        # A `null` on it is a return to the reviewed default, under the propose rule: a release
        # that marks a tuned key `never` has to leave a way back to the value review chose.
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"threshold": None}, reason="back")
        self.assertIn("confirmed_by", str(ctx.exception))
        entry = cs.apply_changes(self.root, "demo", {"threshold": None}, reason="back", confirmed_by="ops")
        self.assertEqual(entry["unset"], {"threshold": {"before": 6, "after": 3}})
        self.assertNotIn("threshold", cs.load(self.root, "demo").criteria)

    def test_the_whole_call_is_refused_if_any_key_is(self):
        seed(self.root, learning={"keys": {"names": "autonomous", "threshold": "never"}})
        with self.assertRaises(cs.CapabilityError):
            cs.apply_changes(self.root, "demo", {"names": ["a"], "threshold": 9}, reason="x", confirmed_by="ops")
        self.assertEqual(cs.load(self.root, "demo").criteria, {"threshold": 3})

    def test_a_capability_without_its_schema_is_refused_not_validated(self):
        # A torn first scaffold or a hand-made directory: criteria.json present, no schema. Every
        # key and value would validate against an empty schema, so the store refuses the
        # capability for reads and writes alike, naming the missing file.
        d = self.root / "torn"
        d.mkdir(parents=True)
        (d / cs.CRITERIA_FILENAME).write_text(json.dumps({"threshold": 3}))
        self.assertEqual(cs.list_capabilities(self.root), ["torn"])
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "torn", {"anything": {"nested": True}}, reason="x", confirmed_by="ops@example")
        self.assertIn(cs.SCHEMA_FILENAME, str(ctx.exception))
        self.assertEqual(json.loads((d / cs.CRITERIA_FILENAME).read_text()), {"threshold": 3})
        self.assertFalse((d / cs.CHANGELOG_FILENAME).exists())
        with self.assertRaises(cs.CapabilityError):
            cs.load(self.root, "torn")
        (d / cs.SCHEMA_FILENAME).write_text("[]")
        with self.assertRaises(cs.CapabilityError):
            cs.load(self.root, "torn")

    def test_an_unreadable_criteria_file_names_its_recovery(self):
        # The scaffold holds a volume criteria file it cannot read rather than replacing it, so a
        # restart does not recover this one; the refusal has to say what does.
        seed(self.root)
        first = cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="x", confirmed_by="ops@example")
        (self.root / "demo" / cs.CRITERIA_FILENAME).write_text("{not json")
        for call in (lambda: cs.load(self.root, "demo"),
                     lambda: cs.apply_changes(self.root, "demo", {"threshold": 5}, reason="y", confirmed_by="ops@example")):
            with self.assertRaises(cs.CapabilityError) as ctx:
                call()
            self.assertIn("remove the file", str(ctx.exception))
            self.assertIn(cs.CRITERIA_FILENAME, str(ctx.exception))
        # The trail the message points at is readable in that state, and after the file is removed.
        self.assertEqual(cs.history(self.root, "demo"), [first])
        (self.root / "demo" / cs.CRITERIA_FILENAME).unlink()
        self.assertEqual(cs.history(self.root, "demo"), [first])
        self.assertEqual(cs.list_capabilities(self.root), [], "without its criteria file the capability is not listed")
        with self.assertRaises(cs.CapabilityError):
            cs.history(self.root, "nope")

    def test_a_capability_without_its_learning_policy_is_refused_not_downgraded(self):
        # Without the policy file every key would read as `propose`, and a key the image marks
        # `never` would take a confirmed write. Refused like the schema, naming the file.
        seed(self.root)
        (self.root / "demo" / cs.LEARNING_FILENAME).unlink()
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="x", confirmed_by="ops@example")
        self.assertIn(cs.LEARNING_FILENAME, str(ctx.exception))
        self.assertEqual(json.loads((self.root / "demo" / cs.CRITERIA_FILENAME).read_text()), {"threshold": 3})
        (self.root / "demo" / cs.LEARNING_FILENAME).write_text("[]")
        with self.assertRaises(cs.CapabilityError):
            cs.load(self.root, "demo")

    def test_the_next_revision_counts_past_an_orphan_changelog_line(self):
        # A line whose value write failed keeps its number; the next accepted write takes the one
        # after it, so history never shows two changes at one revision.
        seed(self.root)
        with patch.object(cs, "write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="x", confirmed_by="ops@example")
        entry = cs.apply_changes(self.root, "demo", {"threshold": 5}, reason="y", confirmed_by="ops@example")
        self.assertEqual(entry["revision"], 2)
        self.assertEqual(cs.load(self.root, "demo").criteria[cs.REVISION_KEY], 2)
        self.assertEqual([e["revision"] for e in cs.history(self.root, "demo")], [1, 2])

    def test_the_changelog_line_lands_before_the_value_moves(self):
        # A changelog the store cannot append to leaves the value where it was; a value the
        # store cannot write leaves the line that says what was about to change.
        seed(self.root)
        (self.root / "demo" / cs.CHANGELOG_FILENAME).mkdir()
        with self.assertRaises(OSError):
            cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="x", confirmed_by="ops@example")
        self.assertEqual(cs.load(self.root, "demo").criteria, {"threshold": 3})
        (self.root / "demo" / cs.CHANGELOG_FILENAME).rmdir()
        with patch.object(cs, "write_json_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="x", confirmed_by="ops@example")
        self.assertEqual(cs.load(self.root, "demo").criteria, {"threshold": 3})
        (line,) = cs.history(self.root, "demo")
        self.assertEqual((line["revision"], line["changes"]), (1, {"threshold": {"before": 3, "after": 4}}))

    def test_a_torn_changelog_tail_is_sealed_not_extended(self):
        # An append that failed part-way (a full disk, a kill mid-flush) leaves the file ending
        # mid-line. The next line has to start on a line of its own: glued onto the fragment it
        # is one line `history` cannot parse, and the accepted change vanishes with the garbage.
        seed(self.root)
        changelog = self.root / "demo" / cs.CHANGELOG_FILENAME
        first = cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="x", confirmed_by="ops@example")
        fragment = json.dumps(dict(first, revision=2), sort_keys=True)[:60]
        with changelog.open("a", encoding="utf-8") as fh:
            fh.write(fragment)
        second = cs.apply_changes(self.root, "demo", {"threshold": 5}, reason="y", confirmed_by="ops@example")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(cs.history(self.root, "demo"), [first, second], "the accepted change is readable")
        lines = changelog.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[1], fragment, "the fragment stays, on a line of its own")
        self.assertEqual(json.loads(lines[2]), second)
        self.assertEqual(lines[3], "", "the file ends in a newline again")
        # A fragment as the whole file, from a first append that failed: sealed the same way.
        seed(self.root, "fresh")
        torn = self.root / "fresh" / cs.CHANGELOG_FILENAME
        torn.write_text(fragment, encoding="utf-8")
        only = cs.apply_changes(self.root, "fresh", {"threshold": 4}, reason="x", confirmed_by="ops@example")
        self.assertEqual(only["revision"], 1)
        self.assertEqual(cs.history(self.root, "fresh"), [only])
        self.assertEqual(torn.read_text(encoding="utf-8").split("\n")[0], fragment)

    def test_a_null_value_returns_a_key_to_the_shipped_default(self):
        # "Set it back" by hand pins the old number on the volume, where the merge keeps it past
        # every later release; `null` removes the key, so the schema default applies again and a
        # release that moves it reaches the install. The same policy gates it as any write.
        seed(self.root, criteria={"threshold": 5, "names": ["a"]})
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"threshold": None}, reason="back to default")
        self.assertIn("propose", str(ctx.exception))
        entry = cs.apply_changes(self.root, "demo", {"threshold": None, "names": ["b"]}, reason="back to default", confirmed_by="ops@example")
        self.assertEqual(entry["unset"], {"threshold": {"before": 5, "after": 3}})
        self.assertEqual(entry["changes"], {"names": {"before": ["a"], "after": ["b"]}})
        cap = cs.load(self.root, "demo")
        self.assertNotIn("threshold", cap.criteria, "the key is gone from the volume, not stored as the default")
        self.assertEqual(cap.criteria[cs.REVISION_KEY], 1)
        self.assertEqual(cs.describe(cap)["criteria"]["threshold"], 3)
        self.assertEqual(cs.history(self.root, "demo"), [entry])
        # A release that moves the default now reaches the install.
        moved = json.loads(json.dumps(SCHEMA))
        moved["properties"]["threshold"]["default"] = 4
        (self.root / "demo" / cs.SCHEMA_FILENAME).write_text(json.dumps(moved))
        self.assertEqual(cs.describe(cs.load(self.root, "demo"))["criteria"]["threshold"], 4)
        # Nothing to remove is said, not silently accepted.
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"threshold": None}, reason="again", confirmed_by="ops@example")
        self.assertIn("no stored value", str(ctx.exception))
        self.assertEqual(cs.load(self.root, "demo").criteria[cs.REVISION_KEY], 1, "a refused call writes nothing")

    def test_a_stale_key_is_returned_to_the_default_with_null_too(self):
        # A key a release dropped is the one `get` lists under stale_keys and the one an operator
        # asks to clear; `null` on it is an unset, not an unknown-key refusal, and the entry says
        # it was asked for rather than pruned as a side effect.
        seed(self.root, criteria={"threshold": 3, "retired": True})
        entry = cs.apply_changes(self.root, "demo", {"retired": None}, reason="clear it", confirmed_by="ops@example")
        self.assertEqual(entry["unset"], {"retired": {"before": True, "after": None}})
        self.assertEqual(entry["pruned"], {})
        self.assertEqual(cs.load(self.root, "demo").criteria, {"threshold": 3, cs.REVISION_KEY: 1, cs.UPDATED_AT_KEY: entry["ts"]})
        with self.assertRaises(cs.CapabilityError):
            cs.apply_changes(self.root, "demo", {"retired": 5}, reason="x", confirmed_by="ops@example")

    def test_a_changelog_line_that_is_not_utf8_is_skipped_not_raised(self):
        seed(self.root)
        first = cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="x", confirmed_by="ops@example")
        with (self.root / "demo" / cs.CHANGELOG_FILENAME).open("ab") as fh:
            fh.write(b"\xff\xfe not a line\n")
        self.assertEqual(cs.history(self.root, "demo"), [first])
        second = cs.apply_changes(self.root, "demo", {"threshold": 5}, reason="y", confirmed_by="ops@example")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(cs.history(self.root, "demo"), [first, second])

    def test_a_pattern_on_a_string_is_enforced(self):
        # The flag that carries a list cannot carry an entry with a comma, a space, a quote or a
        # newline, so the schema refuses it at the store and `get`, the flag and the manifest
        # agree. `\Z`, not `$`: Python's `$` would let the newline through.
        schema = {"type": "object", "additionalProperties": False, "properties": {"names": {"type": "array", "items": {"type": "string", "pattern": r"^[A-Z0-9_]+\Z"}, "default": []}}}
        seed(self.root, criteria={}, schema=schema)
        for bad in ("COS CONTAINERD", "COS,", '"COS"', "COS\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(cs.CapabilityError) as ctx:
                    cs.apply_changes(self.root, "demo", {"names": [bad]}, reason="x", confirmed_by="ops@example")
                self.assertIn("must match", str(ctx.exception))
        cs.apply_changes(self.root, "demo", {"names": ["COS_CONTAINERD"]}, reason="x", confirmed_by="ops@example")
        self.assertEqual(cs.load(self.root, "demo").criteria["names"], ["COS_CONTAINERD"])

    def test_schema_violations_are_named_and_nothing_is_written(self):
        seed(self.root)
        cases = {
            "type": {"threshold": "four"},
            "bool_is_not_integer": {"threshold": True},
            "minimum": {"threshold": 0},
            "maximum": {"threshold": 11},
            "enum": {"mode": "shout"},
            "item_type": {"names": [1]},
            "max_items": {"names": ["a", "b", "c", "d"]},
            "unknown_key": {"colour": "red"},
        }
        for label, change in cases.items():
            with self.subTest(label):
                with self.assertRaises(cs.CapabilityError) as ctx:
                    cs.apply_changes(self.root, "demo", change, reason="x", confirmed_by="ops")
                self.assertIn("invalid", str(ctx.exception))
        self.assertEqual(cs.load(self.root, "demo").criteria, {"threshold": 3})

    def test_an_unknown_key_is_reported_as_unknown_not_as_a_policy_refusal(self):
        seed(self.root)
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"treshold": 4}, reason="typo")
        msg = str(ctx.exception)
        self.assertIn("treshold", msg)
        self.assertIn("defined keys", msg)
        self.assertNotIn("propose", msg)

    def test_a_key_the_schema_dropped_is_pruned_on_the_next_write_not_a_wedge(self):
        # The volume-wins merge keeps a key across the release that removed it.
        # Refusing every later set over it would leave the capability untunable
        # with nothing the tool could name to fix that.
        seed(self.root, criteria={"threshold": 3, "retired": True, cs.REVISION_KEY: 2})
        got = cs.describe(cs.load(self.root, "demo"))
        self.assertEqual(got["stale_keys"], ["retired"])
        self.assertNotIn("retired", got["criteria"])
        entry = cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="r", confirmed_by="ops")
        self.assertEqual(entry["pruned"], {"retired": True})
        self.assertEqual(cs.load(self.root, "demo").criteria, {"threshold": 4, cs.REVISION_KEY: 3,
                                                                cs.UPDATED_AT_KEY: entry["ts"]})

    def test_only_criteria_and_the_changelog_are_ever_written(self):
        seed(self.root, learning={"default": "propose"})
        before = {p.name: p.read_text() for p in (self.root / "demo").iterdir()}
        cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="r", confirmed_by="ops")
        after = {p.name: p.read_text() for p in (self.root / "demo").iterdir()}
        self.assertEqual(after[cs.SCHEMA_FILENAME], before[cs.SCHEMA_FILENAME])
        self.assertEqual(after[cs.LEARNING_FILENAME], before[cs.LEARNING_FILENAME])
        self.assertEqual(
            set(after) - set(before), {cs.CHANGELOG_FILENAME, cs.LOCK_FILENAME},
            "a set creates the changelog and the lock file and nothing else",
        )

    def test_a_stored_value_the_schema_now_rejects_is_reported_and_reset_not_effective(self):
        # A release tightened `minimum` after the volume stored 1. The merge kept
        # the value; it must not run silently, and it must not wedge later sets.
        tighter = json.loads(json.dumps(SCHEMA))
        tighter["properties"]["threshold"]["minimum"] = 2
        seed(self.root, criteria={"threshold": 1, "names": ["a"]}, schema=tighter)
        got = cs.describe(cs.load(self.root, "demo"))
        self.assertEqual(got["criteria"]["threshold"], 3, "the schema default stands in")
        self.assertIn("threshold", got["invalid_keys"])
        entry = cs.apply_changes(self.root, "demo", {"names": ["b"]}, reason="r", confirmed_by="ops")
        self.assertEqual(entry["reset"]["threshold"]["value"], 1)
        self.assertNotIn("threshold", cs.load(self.root, "demo").criteria)
        entry = cs.apply_changes(self.root, "demo", {"threshold": 5}, reason="r", confirmed_by="ops")
        self.assertEqual(entry["reset"], {}, "nothing invalid was left to record")
        self.assertEqual(cs.load(self.root, "demo").criteria["threshold"], 5)

    def test_setting_an_invalid_key_itself_still_records_the_raw_value_it_replaced(self):
        tighter = json.loads(json.dumps(SCHEMA))
        tighter["properties"]["threshold"]["minimum"] = 2
        seed(self.root, criteria={"threshold": 1}, schema=tighter)
        entry = cs.apply_changes(self.root, "demo", {"threshold": 5}, reason="r", confirmed_by="ops")
        self.assertEqual(entry["changes"], {"threshold": {"before": 3, "after": 5}}, "before is what was in effect")
        self.assertEqual(entry["reset"]["threshold"]["value"], 1, "the raw value is not lost")
        self.assertEqual(cs.load(self.root, "demo").criteria["threshold"], 5)

    def test_changes_may_arrive_json_encoded(self):
        seed(self.root)
        entry = cs.apply_changes(self.root, "demo", '{"threshold": 4}', reason="r", confirmed_by="ops")
        self.assertEqual(entry["changes"], {"threshold": {"before": 3, "after": 4}})
        with self.assertRaises(cs.CapabilityError):
            cs.apply_changes(self.root, "demo", "{not json", reason="r", confirmed_by="ops")

    def test_the_cli_falls_back_to_the_platform_profiles_store(self):
        machine_home = self.root / "home"
        store = machine_home / cs.PLATFORM_PROFILE_SUBDIR / cs.CAPABILITIES_DIRNAME
        seed(store)
        with patch.dict(os.environ, {cs.HERMES_HOME_ENV: str(machine_home)}, clear=True):
            self.assertEqual(cs.cli_root(), store)
        # A `kubectl exec` shell carries PLATFORM_AGENT_HOME and not HERMES_HOME; the same store.
        with patch.dict(os.environ, {cs.PLATFORM_AGENT_HOME_ENV: str(machine_home)}, clear=True):
            self.assertEqual(cs.cli_root(), store)
        (machine_home / cs.CAPABILITIES_DIRNAME).mkdir()
        with patch.dict(os.environ, {cs.HERMES_HOME_ENV: str(machine_home)}, clear=True):
            self.assertEqual(cs.cli_root(), machine_home / cs.CAPABILITIES_DIRNAME, "an existing home store wins")

    def test_the_cli_asks_for_a_name_rather_than_failing_on_argv(self):
        # `get` without a name is the first thing an operator types before `list`; the answer
        # is the usage line, which names `list`, not an index error.
        seed(self.root)
        with patch.dict(os.environ, {cs.CAPABILITIES_DIR_ENV: str(self.root)}):
            for argv in (["capability_store.py", "get"], ["capability_store.py", "history"], ["capability_store.py"], ["capability_store.py", "nope"]):
                with self.subTest(argv=argv):
                    err = io.StringIO()
                    with redirect_stderr(err), redirect_stdout(io.StringIO()):
                        self.assertEqual(cs.main(argv), cs.USAGE_EXIT_CODE)
                    self.assertIn("usage:", err.getvalue())
                    self.assertIn("list", err.getvalue())
                    self.assertNotIn("index", err.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cs.main(["capability_store.py", "get", "demo"]), 0)
            self.assertEqual(json.loads(out.getvalue())["capability"], "demo")
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(cs.main(["capability_store.py", "get", "nope"]), 1)
            self.assertIn("unknown capability", err.getvalue())

    def test_confirmed_by_is_a_name_not_a_transcript(self):
        seed(self.root)
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="r", confirmed_by="x" * 201)
        self.assertIn("confirmed_by", str(ctx.exception))
        self.assertFalse((self.root / "demo" / cs.CHANGELOG_FILENAME).exists())

    def test_state_keys_cannot_be_set_and_a_reason_is_required(self):
        seed(self.root)
        with self.assertRaises(cs.CapabilityError):
            cs.apply_changes(self.root, "demo", {cs.REVISION_KEY: 99}, reason="x", confirmed_by="ops")
        with self.assertRaises(cs.CapabilityError):
            cs.apply_changes(self.root, "demo", {"threshold": 4}, reason="  ", confirmed_by="ops")

    def test_unknown_capability_and_bad_names_are_refused(self):
        seed(self.root)
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.load(self.root, "nope")
        self.assertIn("demo", str(ctx.exception))
        # A root that does not exist is named, so a shell that looked in the wrong place can tell.
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.load(self.root / "missing", "demo")
        self.assertIn(str(self.root / "missing"), str(ctx.exception))
        with self.assertRaises(cs.CapabilityError):
            cs.apply_changes(self.root, "nope", {"threshold": 4}, reason="x", confirmed_by="ops")
        for bad in ("", "../demo", "a/b", ".hidden"):
            with self.subTest(bad):
                with self.assertRaises(cs.CapabilityError):
                    cs.load(self.root, bad)

    def test_list_names_only_directories_holding_criteria(self):
        seed(self.root, "b")
        seed(self.root, "a")
        (self.root / "not-one").mkdir()
        self.assertEqual(cs.list_capabilities(self.root), ["a", "b"])
        self.assertEqual(cs.list_capabilities(self.root / "missing"), [])

    def test_revision_counts_up_across_calls(self):
        seed(self.root)
        for n in (1, 2, 3):
            cs.apply_changes(self.root, "demo", {"threshold": n + 3}, reason="r", confirmed_by="ops")
        self.assertEqual(cs.load(self.root, "demo").criteria[cs.REVISION_KEY], 3)
        self.assertEqual([e["revision"] for e in cs.history(self.root, "demo")], [1, 2, 3])

    def test_root_resolution_prefers_the_env_override(self):
        with patch.dict(os.environ, {cs.CAPABILITIES_DIR_ENV: "/x/y"}):
            self.assertEqual(cs.capabilities_root(), Path("/x/y"))
        with patch.dict(os.environ, {cs.HERMES_HOME_ENV: "/opt/data/profiles/platform"}, clear=True):
            self.assertEqual(cs.capabilities_root(), Path("/opt/data/profiles/platform/capabilities"))
        with patch.dict(os.environ, {cs.PLATFORM_AGENT_HOME_ENV: "/var/agent"}, clear=True):
            self.assertEqual(cs.capabilities_root(), Path("/var/agent/capabilities"))
        with patch.dict(os.environ, {cs.HERMES_HOME_ENV: "/h", cs.PLATFORM_AGENT_HOME_ENV: "/var/agent"}, clear=True):
            self.assertEqual(cs.capabilities_root(), Path("/h/capabilities"), "HERMES_HOME wins when both are set")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cs.capabilities_root(), Path(cs.DEFAULT_HERMES_HOME) / cs.CAPABILITIES_DIRNAME)


class ShippedTemplatesTest(unittest.TestCase):
    """Every directory under agents/platform/capabilities/ has to be loadable and valid on day one."""

    def test_every_shipped_capability_validates_against_its_schema(self):
        names = cs.list_capabilities(SHIPPED)
        self.assertTrue(names, f"no capability templates found under {SHIPPED}")
        for name in names:
            with self.subTest(name):
                cap = cs.load(SHIPPED, name)
                self.assertTrue(cap.schema, f"{name} ships no {cs.SCHEMA_FILENAME}")
                self.assertEqual(cs.validate(cap.schema, cap.criteria), [])
                self.assertEqual(cap.learning.get("default"), cs.POLICY_PROPOSE)
                for key, policy in (cap.learning.get("keys") or {}).items():
                    self.assertIn(policy, cs.POLICIES, f"{name}: {key}")
                    self.assertIn(key, cap.schema["properties"], f"{name}: policy for undefined key {key}")
                self.assertFalse(any(k in cs.STATE_KEYS for k in cap.criteria), f"{name}: template carries state")

    def test_the_procedure_and_the_schema_name_the_same_keys(self):
        # The SOP reads `criteria.<key>`; a token the schema does not define means
        # the run silently falls back to the number printed on the page, and a
        # schema key the SOP never reads is a knob that turns nothing.
        governance = REPO_ROOT / "agents" / "platform" / "governance"
        token = re.compile(r"`criteria\.([a-z0-9_]+)`")
        for name in cs.list_capabilities(SHIPPED):
            sop = governance / (name.replace("-", "_") + "_sop.md")
            if not sop.is_file():
                continue
            with self.subTest(name):
                cited = set(token.findall(sop.read_text(encoding="utf-8")))
                defined = set(cs.load(SHIPPED, name).properties())
                self.assertEqual(cited, defined)

    def test_every_shipped_criteria_file_is_empty(self):
        # The day-one value is the schema's `default`, read at use time, and the
        # volume holds only what an operator tuned. Ship a populated
        # criteria.json and the first scaffold copies every key onto the volume,
        # where merge_volume_wins then pins it forever: no later release can
        # correct a default, because the merge cannot tell a value the operator
        # chose from one the image handed over on day one.
        for name in cs.list_capabilities(SHIPPED):
            with self.subTest(name):
                self.assertEqual(cs.load(SHIPPED, name).criteria, {})

    def test_every_shipped_schema_is_closed(self):
        # additionalProperties: false is the ONLY thing that refuses an unknown
        # key. A schema that omits it accepts anything the model invents, and
        # every other guard in validate() still passes.
        for name in cs.list_capabilities(SHIPPED):
            with self.subTest(name):
                cap = cs.load(SHIPPED, name)
                self.assertIs(cap.schema.get("additionalProperties"), False)
                self.assertTrue(
                    all("default" in spec for spec in cap.schema["properties"].values()),
                    f"{name}: every property needs a default, since the template ships none",
                )

    def test_a_shipped_pattern_refuses_a_trailing_newline(self):
        # JSON Schema's `pattern` is ECMA-262, where `$` is the end of the input; the validator
        # is Python's `re`, whose `$` also matches before a final newline. So a shipped pattern
        # anchors its end with `\Z`, and the image-type entry the flag's grammar would split on
        # a newline is refused at the store, like the comma, the space and the quote.
        for name in cs.list_capabilities(SHIPPED):
            for key, spec in cs.load(SHIPPED, name).properties().items():
                for pattern in (spec.get("pattern"), (spec.get("items") or {}).get("pattern")):
                    if pattern is not None:
                        with self.subTest(name=name, key=key):
                            self.assertFalse(pattern.endswith("$"), f"{pattern}: anchor the end with \\Z, not $")
        schema = cs.load(SHIPPED, "security-patch-orchestrator").schema
        for bad in ("COS\n", "COS CONTAINERD", "COS,", '"COS"', "[COS]"):
            with self.subTest(bad=bad):
                self.assertTrue(cs.validate(schema, {"deprecated_image_types": [bad]}))
        for good in ("COS", "cos_containerd", "WINDOWS_SAC"):
            with self.subTest(good=good):
                self.assertEqual(cs.validate(schema, {"deprecated_image_types": [good]}), [])

    def test_every_shipped_capability_is_a_cron_job_on_the_platform_roster(self):
        roster = json.loads((REPO_ROOT / "agents" / "platform" / "cron" / "jobs.json").read_text())
        ids = {j["id"] for j in roster["jobs"]}
        for name in cs.list_capabilities(SHIPPED):
            with self.subTest(name):
                self.assertIn(name, ids, "a capability is named after the job that runs it")


if __name__ == "__main__":
    unittest.main()
