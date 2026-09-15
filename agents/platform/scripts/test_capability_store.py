"""Unit tests for capability_store: the runtime-owned criteria behind the delivery vehicle.

Run: python3 -m unittest agents.platform.scripts.test_capability_store
"""

import json
import os
import re
import sys
import tempfile
import unittest
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
    if learning is not None:
        (d / cs.LEARNING_FILENAME).write_text(json.dumps(learning))
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
        seed(self.root, learning={"keys": {"threshold": "never"}})
        with self.assertRaises(cs.CapabilityError) as ctx:
            cs.apply_changes(self.root, "demo", {"threshold": 9}, reason="x", confirmed_by="ops")
        self.assertIn("never", str(ctx.exception))

    def test_the_whole_call_is_refused_if_any_key_is(self):
        seed(self.root, learning={"keys": {"names": "autonomous", "threshold": "never"}})
        with self.assertRaises(cs.CapabilityError):
            cs.apply_changes(self.root, "demo", {"names": ["a"], "threshold": 9}, reason="x", confirmed_by="ops")
        self.assertEqual(cs.load(self.root, "demo").criteria, {"threshold": 3})

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
        (machine_home / cs.CAPABILITIES_DIRNAME).mkdir()
        with patch.dict(os.environ, {cs.HERMES_HOME_ENV: str(machine_home)}, clear=True):
            self.assertEqual(cs.cli_root(), machine_home / cs.CAPABILITIES_DIRNAME, "an existing home store wins")

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

    def test_shipped_defaults_equal_the_schema_defaults(self):
        # The template is the day-one value and the schema's `default` is what a
        # reset returns to; the two saying different things is a trap for both.
        for name in cs.list_capabilities(SHIPPED):
            with self.subTest(name):
                cap = cs.load(SHIPPED, name)
                self.assertEqual(cap.criteria, cap.defaults())

    def test_every_shipped_capability_is_a_cron_job_on_the_platform_roster(self):
        roster = json.loads((REPO_ROOT / "agents" / "platform" / "cron" / "jobs.json").read_text())
        ids = {j["id"] for j in roster["jobs"]}
        for name in cs.list_capabilities(SHIPPED):
            with self.subTest(name):
                self.assertIn(name, ids, "a capability is named after the job that runs it")


if __name__ == "__main__":
    unittest.main()
