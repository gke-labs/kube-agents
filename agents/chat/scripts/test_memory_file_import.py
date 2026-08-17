"""Unit tests for the file-store migration.

Run: python3 -m unittest agents/chat/scripts/test_memory_file_import.py

The theme of these tests is that the delete must never outrun the import. Every
path that removes a file is checked twice: once for removing it when the bank
really holds every entry, and once for refusing when it does not.

Hindsight is stubbed. What is exercised is the decision logic — what gets
retained, under which tag, and whether the file survives — not HTTP.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import memory_file_import as mfi  # noqa: E402

BANK = "kube-agents-memory"

# A filename in the exact shape the deployment this migration exists for
# produces: the sanitizer turned '@' into '_', and the twelve hex characters are
# sha256 of the raw id. The three constants are a real, self-consistent triple —
# recovering the stem must yield the raw id, which must hash back to the digest.
KNOWN_STEM = "alice_example.com_ff8d9819fc0e"
KNOWN_RAW = "alice@example.com"
# The scope tag that identity resolves to. The digest is the same twelve
# characters as the filename's, because both hash the raw id.
KNOWN_TAG = "user:alice-example-com_ff8d9819fc0e"


class FakeHindsight:
    """Records retains and reports whichever contexts the test says landed.

    `swallow` names the entry indices whose retain produces no memory unit —
    the extractor deciding the content is not durable. That is the case the
    delete gate exists for, and the only way to reach it is to simulate it.
    """

    def __init__(self, swallow=()):
        self.retained = []
        self.consolidated = 0
        self._swallow = set(swallow)

    def landed(self, bank_id):
        # Derived from the contexts actually retained, so the round trip through
        # the real context format and `key_of` is part of every test.
        return {mfi.key_of(item["context"])
                for i, item in enumerate(self.retained) if i not in self._swallow}

    def retain(self, bank_id, item):
        self.retained.append(item)
        return {}

    def consolidate(self, bank_id):
        self.consolidated += 1
        return {}


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative, entries):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mfi.ENTRY_DELIMITER.join(entries), encoding="utf-8")
        return path


class ParsingTest(TempHome):
    def test_entries_split_on_the_delimiter(self):
        path = self.write("MEMORY.md", ["first fact", "second fact", "third fact"])
        self.assertEqual(mfi.read_entries(path), ["first fact", "second fact", "third fact"])

    def test_blank_entries_and_surrounding_whitespace_are_dropped(self):
        path = self.home / "MEMORY.md"
        path.write_text(f"\n  a fact  {mfi.ENTRY_DELIMITER}   {mfi.ENTRY_DELIMITER}b fact\n")
        self.assertEqual(mfi.read_entries(path), ["a fact", "b fact"])

    def test_an_empty_file_has_no_entries(self):
        path = self.home / "MEMORY.md"
        path.write_text("   \n")
        self.assertEqual(mfi.read_entries(path), [])


class OwnerRecoveryTest(unittest.TestCase):
    def test_recovers_the_raw_id_from_the_stored_filename(self):
        self.assertEqual(mfi.recover_raw_user_id(KNOWN_STEM), KNOWN_RAW)

    def test_recovered_id_produces_the_tag_the_provider_will_read(self):
        raw = mfi.recover_raw_user_id(KNOWN_STEM)
        self.assertEqual(f"{mfi.USER_TAG_PREFIX}{mfi.sanitize_user_id(raw)}", KNOWN_TAG)

    def test_identities_that_sanitize_alike_still_get_different_tags(self):
        # The readable half is lossy and the tag is the whole isolation boundary,
        # so these two people must not share one. Email-shaped ids differing only
        # in punctuation are the realistic case, not a contrived one.
        first = mfi.sanitize_user_id("alice.smith@corp.example")
        second = mfi.sanitize_user_id("alice-smith@corp.example")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("alice-smith-corp-example_"))

    def test_an_empty_identity_sanitizes_to_nothing(self):
        # Not to a hash of the empty string: the caller reads "" as "no identity"
        # and refuses to touch personal memory on it.
        self.assertEqual(mfi.sanitize_user_id(""), "")
        self.assertEqual(mfi.sanitize_user_id("   "), "")

    def test_an_id_that_needed_no_substitution_still_verifies(self):
        raw = "slackbot"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
        self.assertEqual(mfi.recover_raw_user_id(f"{raw}_{digest}"), raw)

    def test_a_literal_underscore_in_the_raw_id_is_recovered(self):
        raw = "team_ops@corp.example"
        sanitized = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw)
        digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
        self.assertEqual(mfi.recover_raw_user_id(f"{sanitized}_{digest}"), raw)

    def test_a_digest_that_matches_nothing_returns_none(self):
        # Never guess an owner: the wrong tag is a leak, no tag is a silent loss.
        self.assertIsNone(mfi.recover_raw_user_id("someone_000000000000"))

    def test_a_filename_with_no_digest_returns_none(self):
        self.assertIsNone(mfi.recover_raw_user_id("MEMORY"))


class ContextKeyTest(unittest.TestCase):
    def setUp(self):
        self.source = mfi.Source(Path("MEMORY.md"), "MEMORY.md",
                                 mfi.SHARED_TAG, mfi.SHARED_STRATEGY)

    def test_a_context_round_trips_to_its_key(self):
        context = self.source.context_for("a fact", 4)
        self.assertIn("MEMORY.md entry 5", context)
        self.assertEqual(mfi.key_of(context), self.source.key_for("a fact"))

    def test_the_key_ignores_position(self):
        self.assertEqual(mfi.key_of(self.source.context_for("a fact", 0)),
                         mfi.key_of(self.source.context_for("a fact", 9)))

    def test_the_same_entry_in_two_files_has_two_keys(self):
        other = mfi.Source(Path("USER.md"), "USER.md", "user:someone", mfi.PERSONAL_STRATEGY)
        self.assertNotEqual(self.source.key_for("a fact"), other.key_for("a fact"))

    def test_a_context_this_script_did_not_write_is_not_ours(self):
        # The bank is shared with everything the agent retains normally.
        self.assertIsNone(mfi.key_of("a conversation with the user about clusters"))
        self.assertIsNone(mfi.key_of(""))


class DiscoveryTest(TempHome):
    def test_both_layouts_are_found_with_the_right_scope(self):
        self.write("MEMORY.md", ["builtin shared"])
        self.write("memories/MEMORY.md", ["multiuser shared"])
        self.write(f"memories/users/{KNOWN_STEM}.md", ["personal"])

        sources, skipped = mfi.discover(self.home, None)
        by_label = {s.label: s for s in sources}

        self.assertEqual(by_label["MEMORY.md"].tag, mfi.SHARED_TAG)
        self.assertEqual(by_label["MEMORY.md"].strategy, mfi.SHARED_STRATEGY)
        self.assertEqual(by_label["memories/MEMORY.md"].tag, mfi.SHARED_TAG)

        personal = by_label[f"memories/users/{KNOWN_STEM}.md"]
        self.assertEqual(personal.tag, KNOWN_TAG)
        self.assertEqual(personal.strategy, mfi.PERSONAL_STRATEGY)
        self.assertEqual(skipped, [])

    def test_builtin_user_file_is_skipped_without_an_identity(self):
        self.write("USER.md", ["a preference"])
        sources, skipped = mfi.discover(self.home, None)
        self.assertEqual(sources, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("USER.md", skipped[0])

    def test_builtin_user_file_is_migrated_when_told_whose_it_is(self):
        self.write("USER.md", ["a preference"])
        sources, skipped = mfi.discover(self.home, KNOWN_RAW)
        self.assertEqual([s.tag for s in sources], [KNOWN_TAG])
        self.assertEqual(skipped, [])

    def test_an_unrecoverable_owner_is_reported_not_guessed(self):
        self.write("memories/users/mystery_000000000000.md", ["something"])
        sources, skipped = mfi.discover(self.home, None)
        self.assertEqual(sources, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("mystery", skipped[0])

    def test_a_migrated_volume_has_nothing_to_do(self):
        sources, skipped = mfi.discover(self.home, None)
        self.assertEqual((sources, skipped), ([], []))


class MigrationTest(TempHome):
    def source(self, relative="MEMORY.md", entries=("alpha", "beta", "gamma")):
        path = self.write(relative, list(entries))
        return mfi.Source(path, relative, mfi.SHARED_TAG, mfi.SHARED_STRATEGY)

    def test_every_entry_is_its_own_retain_call(self):
        # Batching is what defect #111 is: Hindsight collapses a multi-item
        # retain into one document and keeps a single item's context for all.
        api = FakeHindsight()
        source = self.source()
        mfi.migrate(api, BANK, self.home, source, landed=set(), commit=True)

        self.assertEqual([i["content"] for i in api.retained], ["alpha", "beta", "gamma"])
        self.assertEqual({i["tags"][0] for i in api.retained}, {mfi.SHARED_TAG})
        self.assertEqual({i["strategy"] for i in api.retained}, {mfi.SHARED_STRATEGY})
        for item in api.retained:
            self.assertEqual(item["observation_scopes"], [[mfi.SHARED_TAG]])
        self.assertEqual(len({i["context"] for i in api.retained}), 3)

    def test_a_verified_file_is_removed_and_leaves_a_receipt(self):
        api = FakeHindsight()
        source = self.source()
        original = source.path.read_bytes()

        result = mfi.migrate(api, BANK, self.home, source, landed=set(), commit=True)

        self.assertTrue(result["removed"])
        self.assertFalse(source.path.exists())

        receipt = json.loads(mfi.receipt_path(self.home, source).read_text())
        self.assertEqual(receipt["entries"], 3)
        self.assertEqual(receipt["sha256"], hashlib.sha256(original).hexdigest())
        # The whole point is that the content stops being readable here.
        self.assertNotIn("alpha", json.dumps(receipt))

    def test_an_entry_that_produced_no_unit_keeps_the_file(self):
        api = FakeHindsight(swallow=[1])
        source = self.source()

        result = mfi.migrate(api, BANK, self.home, source, landed=set(), commit=True)

        self.assertFalse(result["removed"])
        self.assertTrue(source.path.exists())
        self.assertEqual(result["missing"], 1)
        self.assertIn("2", result["note"])
        self.assertFalse(mfi.receipt_path(self.home, source).exists())

    def test_a_resumed_run_does_not_retain_what_already_landed(self):
        api = FakeHindsight()
        first = self.source()
        landed = set()
        mfi.migrate(api, BANK, self.home, first, landed=landed, commit=True)

        # Same content, as it would be if the file had never been deleted.
        again = self.source()
        result = mfi.migrate(api, BANK, self.home, again, landed=landed, commit=True)

        self.assertEqual(len(api.retained), 3)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["already"], 3)
        self.assertTrue(result["removed"])

    def test_an_edited_store_only_imports_what_is_new(self):
        # Contexts are content-addressed, so removing an entry and shifting the
        # rest up must not look like three new entries.
        api = FakeHindsight()
        landed = set()
        mfi.migrate(api, BANK, self.home, self.source(), landed=landed, commit=True)

        shifted = self.source(entries=("alpha", "gamma", "delta"))
        result = mfi.migrate(api, BANK, self.home, shifted, landed=landed, commit=True)

        self.assertEqual(result["imported"], 1)
        self.assertEqual(api.retained[-1]["content"], "delta")

    def test_dry_run_writes_nothing_and_deletes_nothing(self):
        api = FakeHindsight()
        source = self.source()

        result = mfi.migrate(api, BANK, self.home, source, landed=set(), commit=False)

        self.assertEqual(api.retained, [])
        self.assertTrue(source.path.exists())
        self.assertFalse(result["removed"])
        self.assertEqual(result["imported"], 3)

    def test_an_empty_store_is_removed_without_a_retain(self):
        source = self.source(entries=())
        source.path.write_text("\n")
        api = FakeHindsight()

        result = mfi.migrate(api, BANK, self.home, source, landed=set(), commit=True)

        self.assertEqual(api.retained, [])
        self.assertTrue(result["removed"])
        self.assertFalse(source.path.exists())


if __name__ == "__main__":
    unittest.main()
