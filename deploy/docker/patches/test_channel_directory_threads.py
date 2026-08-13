"""Unit tests for the channel-directory thread-id fix.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

``STORM`` below is the directory as it actually stood on 2026-08-09: 33 entries,
every one of them a thread in the same DM, every one queued for its own
``conversations.info`` call against an id Slack has never heard of. The number
that matters in this file is 1.
"""

import unittest

import channel_directory_threads as cdt
from channel_directory_threads import (
    MAX_PROBES_PER_REFRESH,
    MISS_TTL_SECONDS,
    base_channel_id,
    looks_like_raw_id,
    note_resolved,
    note_unresolvable,
    rename_entry,
    unresolved_by_channel,
)

DM = "D0BKGRBM6RH"

#: The directory that produced the storm. Thread ids are the real ones' shape:
#: a Slack message ts, which is what makes the composite id unresolvable.
STORM = [
    {
        "id": f"{DM}:17862404{38 + i}.796879",
        "name": f"{DM} / topic 17862404{38 + i}.796879",
        "type": "dm",
    }
    for i in range(33)
]


class BaseChannelIdTest(unittest.TestCase):
    def test_a_thread_qualified_id_yields_the_channel(self):
        self.assertEqual(base_channel_id(f"{DM}:1786240438.796879"), DM)

    def test_a_plain_id_is_unchanged(self):
        self.assertEqual(base_channel_id(DM), DM)

    def test_absent_and_non_string_ids_do_not_raise(self):
        # Called on every entry in the directory, including ones built by code
        # this patch never saw.
        self.assertEqual(base_channel_id(None), "")
        self.assertEqual(base_channel_id(""), "")
        self.assertEqual(base_channel_id(12345), "12345")


class RenameEntryTest(unittest.TestCase):
    def test_the_thread_label_survives_the_rename(self):
        """Two threads in one DM are only told apart by the part after the slash."""
        entry = {"id": f"{DM}:1786240438.796879", "name": f"{DM} / topic 1786240438.796879"}
        rename_entry(entry, "adamparco", "dm")
        self.assertEqual(entry["name"], "adamparco / topic 1786240438.796879")
        self.assertEqual(entry["type"], "dm")

    def test_an_entry_with_no_thread_is_renamed_whole(self):
        entry = {"id": DM, "name": DM}
        rename_entry(entry, "adamparco")
        self.assertEqual(entry["name"], "adamparco")

    def test_an_already_readable_name_is_left_alone(self):
        """Every entry on a channel is renamed, so this has to be idempotent.

        The resolved name is applied to the whole group without first asking
        which members still needed it; a second refresh must not turn
        "adamparco / topic 1" into "adamparco / adamparco / topic 1".
        """
        entry = {"id": f"{DM}:1", "name": "adamparco / topic 1"}
        rename_entry(entry, "someone-else")
        self.assertEqual(entry["name"], "adamparco / topic 1")

    def test_an_empty_resolution_changes_nothing(self):
        entry = {"id": DM, "name": DM}
        rename_entry(entry, "")
        self.assertEqual(entry["name"], DM)

    def test_the_type_is_only_set_when_given(self):
        entry = {"id": DM, "name": DM, "type": "channel"}
        rename_entry(entry, "general")
        self.assertEqual(entry["type"], "channel")


class UnresolvedByChannelTest(unittest.TestCase):
    def setUp(self):
        cdt._misses.clear()
        self.addCleanup(cdt._misses.clear)

    def test_the_storm_collapses_to_one_probe(self):
        """The whole point. 33 entries, 1 channel, 1 API call."""
        groups = unresolved_by_channel(STORM)
        self.assertEqual(len(groups), 1)
        channel_id, entries = groups[0]
        self.assertEqual(channel_id, DM)
        self.assertEqual(len(entries), 33)

    def test_the_probe_is_addressed_to_the_channel_not_the_thread(self):
        """The bug itself: Slack answers channel_not_found for a composite id."""
        (channel_id, _), = unresolved_by_channel(STORM)
        self.assertNotIn(":", channel_id)

    def test_every_entry_on_the_channel_gets_the_answer(self):
        (_, entries), = unresolved_by_channel(STORM)
        for entry in entries:
            rename_entry(entry, "adamparco", "dm")
        self.assertTrue(all(e["name"].startswith("adamparco / ") for e in STORM))
        self.assertFalse(any(looks_like_raw_id(e["name"]) for e in STORM))
        # Restore: STORM is module state shared with the other tests.
        for i, entry in enumerate(STORM):
            entry["name"] = f"{DM} / topic 17862404{38 + i}.796879"

    def test_a_resolved_entry_is_not_queued_again(self):
        resolved = [{"id": f"{DM}:1", "name": "adamparco / topic 1", "type": "dm"}]
        self.assertEqual(unresolved_by_channel(resolved), [])

    def test_distinct_channels_stay_distinct(self):
        mixed = [
            {"id": f"{DM}:1", "name": f"{DM} / topic 1"},
            {"id": "C0DEADBEEF", "name": "C0DEADBEEF"},
            {"id": f"{DM}:2", "name": f"{DM} / topic 2"},
        ]
        groups = dict(unresolved_by_channel(mixed))
        self.assertEqual(sorted(groups), sorted([DM, "C0DEADBEEF"]))
        self.assertEqual(len(groups[DM]), 2)

    def test_first_seen_order_is_kept(self):
        # Not cosmetic: it decides who survives MAX_PROBES_PER_REFRESH.
        mixed = [
            {"id": "C0AAAAAAAA", "name": "C0AAAAAAAA"},
            {"id": "C0BBBBBBBB", "name": "C0BBBBBBBB"},
        ]
        self.assertEqual([cid for cid, _ in unresolved_by_channel(mixed)],
                         ["C0AAAAAAAA", "C0BBBBBBBB"])

    def test_an_entry_with_no_id_is_skipped_rather_than_probed(self):
        self.assertEqual(unresolved_by_channel([{"id": None, "name": "C0X"}]), [])

    def test_a_refresh_is_capped_and_the_cap_is_announced(self):
        many = [
            {"id": f"C0{i:08d}", "name": f"C0{i:08d}"}
            for i in range(MAX_PROBES_PER_REFRESH + 5)
        ]
        with self.assertLogs(cdt.logger, level="WARNING") as logs:
            groups = unresolved_by_channel(many)
        self.assertEqual(len(groups), MAX_PROBES_PER_REFRESH)
        # A silent cap reads like a fully resolved directory.
        self.assertIn("keep their raw ids", "".join(logs.output))


class MissCacheTest(unittest.TestCase):
    def setUp(self):
        cdt._misses.clear()
        self.addCleanup(cdt._misses.clear)

    def test_a_failed_channel_is_not_probed_again(self):
        note_unresolvable(DM, "channel_not_found")
        self.assertEqual(unresolved_by_channel(STORM), [])

    def test_the_cache_is_keyed_by_channel_not_by_entry(self):
        """One failure covers every thread on that channel — the storm again."""
        note_unresolvable(f"{DM}:1786240438.796879", "channel_not_found")
        self.assertEqual(unresolved_by_channel(STORM), [])

    def test_the_suppression_expires(self):
        note_unresolvable(DM, "channel_not_found")
        cdt._misses[DM] -= MISS_TTL_SECONDS + 1
        self.assertEqual(len(unresolved_by_channel(STORM)), 1)

    def test_an_expired_entry_is_dropped_rather_than_kept_forever(self):
        # The cache is the channels currently being backed off, not a log of
        # every channel that ever failed; a gateway runs for the life of the pod.
        note_unresolvable(DM, "channel_not_found")
        cdt._misses[DM] -= MISS_TTL_SECONDS + 1
        unresolved_by_channel(STORM)
        self.assertEqual(cdt._misses, {})

    def test_a_success_clears_the_suppression(self):
        note_unresolvable(DM, "channel_not_found")
        note_resolved(f"{DM}:1786240438.796879")
        self.assertEqual(len(unresolved_by_channel(STORM)), 1)

    def test_suppressed_channels_are_reported_at_debug(self):
        note_unresolvable(DM, "channel_not_found")
        with self.assertLogs(cdt.logger, level="DEBUG") as logs:
            unresolved_by_channel(STORM)
        self.assertIn("skipped 1 channel", "".join(logs.output))

    def test_a_repeat_failure_does_not_re_announce(self):
        with self.assertLogs(cdt.logger, level="DEBUG") as first:
            note_unresolvable(DM, "channel_not_found")
        self.assertIn(DM, "".join(first.output))
        note_unresolvable(DM, "channel_not_found")
        # Nothing new to say; the second call only refreshes the timestamp.
        self.assertEqual(len(cdt._misses), 1)

    def test_an_empty_channel_id_is_not_cached(self):
        note_unresolvable(None, "nonsense")
        self.assertEqual(cdt._misses, {})


if __name__ == "__main__":
    unittest.main()
