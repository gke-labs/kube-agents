"""first_run_report: the one home for the first-run report's names, and its on-demand reader."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))
# bootstrap_delivery.py and bootstrap_handoff.py live beside this module in the image
# (one COPY in deploy/docker/Dockerfile); here they are a directory over.
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "chat" / "scripts"))

import first_run_report as frr  # noqa: E402


class FirstRunReportReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def write(self, name, text):
        (self.home / name).write_text(text, encoding="utf-8")

    def read(self, **kwargs):
        kwargs.setdefault("max_bytes", 4096)
        return frr.read_first_run_report(self.home, **kwargs)

    def test_no_report_file_and_no_marker_is_absent(self):
        out = self.read()
        self.assertEqual(out["state"], frr.STATE_ABSENT)
        self.assertIsNone(out["path_taken"])
        self.assertIsNone(out["report"])
        self.assertFalse(out["findings_registered"])
        self.assertIsNone(out["filed"])

    def test_an_undelivered_ranked_report_is_pending(self):
        self.write(frr.REPORT_NAME, "# Ranked\n")
        out = self.read()
        self.assertEqual(out["state"], frr.STATE_PENDING)
        self.assertEqual(out["report"]["source"], frr.REPORT_NAME)
        self.assertEqual(out["report"]["kind"], frr.WHICH_RANKED)
        self.assertEqual(out["report"]["text"], "# Ranked\n")
        self.assertFalse(out["report"]["truncated"])

    def test_the_delivered_file_is_the_delivered_state_and_wins_over_a_pending_one(self):
        self.write(frr.DELIVERED_REPORT_NAME, "delivered text")
        self.write(frr.REPORT_NAME, "a newer pending one")
        out = self.read()
        self.assertEqual(out["state"], frr.STATE_DELIVERED)
        self.assertEqual(out["report"]["source"], frr.DELIVERED_REPORT_NAME)
        self.assertEqual(out["report"]["text"], "delivered text")

    def test_an_origin_claim_is_delivered_by_its_tag_alone(self):
        # The origin path posts in the claiming run; the rename can fail after the post.
        self.write(frr.COMPLETED_FILE, frr.CLAIM_PATH_ORIGIN + "\n")
        out = self.read()
        self.assertEqual((out["path_taken"], out["state"]), (frr.CLAIM_PATH_ORIGIN, frr.STATE_DELIVERED))
        self.assertIsNone(out["report"])

    def test_a_conversation_claim_is_pending_until_its_post_recorded_an_outcome(self):
        # The conversation path claims first and posts after; a claim whose
        # post has not landed (or whose run died) is still pending, and the
        # report is still INVENTORY.md.
        self.write(frr.COMPLETED_FILE, frr.CLAIM_PATH_CONVERSATION + "\n")
        self.write(frr.REPORT_NAME, "ranked")
        out = self.read()
        self.assertEqual((out["path_taken"], out["state"]), (frr.CLAIM_PATH_CONVERSATION, frr.STATE_PENDING))
        self.write(frr.CONVERSATION_DONE_FILE, "posted\n")
        self.assertEqual(self.read()["state"], frr.STATE_DELIVERED)

    def test_an_empty_claim_from_an_older_build_is_a_null_path(self):
        self.write(frr.COMPLETED_FILE, "")
        self.write(frr.DELIVERED_REPORT_NAME, "x")
        out = self.read()
        self.assertIsNone(out["path_taken"])
        self.assertEqual(out["state"], frr.STATE_DELIVERED)

    def test_a_filed_marker_is_the_filed_state_with_its_record(self):
        self.write(frr.COMPLETED_FILE, frr.CLAIM_PATH_NO_HUMAN + "\n")
        self.write(frr.DELIVERED_REPORT_NAME, "ranked")
        self.write(
            frr.REPORT_FILED_FILE,
            json.dumps({frr.FILED_REPO: "org/gitops", frr.FILED_NUMBER: 12, frr.FILED_URL: "https://x/12", frr.FILED_AT: 1762905600.5}),
        )
        out = self.read()
        self.assertEqual(out["state"], frr.STATE_FILED)
        self.assertEqual(out["path_taken"], frr.CLAIM_PATH_NO_HUMAN)
        self.assertEqual(out["filed"]["repo"], "org/gitops")
        self.assertEqual(out["filed"]["number"], 12)
        self.assertEqual(out["filed"]["url"], "https://x/12")
        self.assertEqual(out["filed"]["filed_at"], "2025-11-12T00:00:00Z")
        self.assertEqual(out["report"]["text"], "ranked")

    def test_a_filed_marker_that_does_not_parse_is_still_filed_with_no_record(self):
        self.write(frr.REPORT_FILED_FILE, "not json")
        out = self.read()
        self.assertEqual(out["state"], frr.STATE_FILED)
        self.assertIsNone(out["filed"])

    def test_a_retired_marker_is_the_retired_state(self):
        self.write(frr.COMPLETED_FILE, frr.CLAIM_PATH_NO_HUMAN + "\n")
        self.write(frr.REPORT_RETIRED_FILE, "1762905600.5\n")
        self.write(frr.DELIVERED_REPORT_NAME, "ranked")
        out = self.read()
        self.assertEqual(out["state"], frr.STATE_RETIRED)
        self.assertEqual(out["retired_at"], "2025-11-12T00:00:00Z")

    def test_the_filed_marker_outranks_the_retired_one(self):
        self.write(frr.REPORT_FILED_FILE, json.dumps({frr.FILED_URL: "https://x/1"}))
        self.write(frr.REPORT_RETIRED_FILE, "1\n")
        self.assertEqual(self.read()["state"], frr.STATE_FILED)

    def test_the_findings_registered_marker_is_reported(self):
        self.write(frr.FINDINGS_REGISTERED_MARKER, "")
        self.assertTrue(self.read()["findings_registered"])

    def test_the_full_report_is_the_raw_file_and_the_state_is_still_the_ranked_reports(self):
        self.write(frr.RAW_REPORT_NAME, "every finding")
        self.write(frr.DELIVERED_REPORT_NAME, "ranked")
        out = self.read(which=frr.WHICH_FULL)
        self.assertEqual(out["state"], frr.STATE_DELIVERED)
        self.assertEqual(out["report"]["kind"], frr.WHICH_FULL)
        self.assertEqual(out["report"]["source"], frr.RAW_REPORT_NAME)
        self.assertEqual(out["report"]["text"], "every finding")

    def test_a_missing_full_report_beside_a_pending_ranked_one(self):
        self.write(frr.REPORT_NAME, "ranked")
        out = self.read(which=frr.WHICH_FULL)
        self.assertEqual(out["state"], frr.STATE_PENDING)
        self.assertIsNone(out["report"])

    def test_a_report_over_the_cap_is_cut_and_marked(self):
        self.write(frr.REPORT_NAME, "abcdefghij")
        out = self.read(max_bytes=4)
        self.assertEqual(out["report"]["text"], "abcd")
        self.assertEqual(out["report"]["bytes"], 4)
        self.assertTrue(out["report"]["truncated"])
        out = self.read(max_bytes=10)
        self.assertEqual(out["report"]["text"], "abcdefghij")
        self.assertFalse(out["report"]["truncated"])

    def test_a_cut_inside_a_multibyte_character_still_decodes(self):
        self.write(frr.REPORT_NAME, "é" * 4)
        out = self.read(max_bytes=3)
        self.assertEqual(out["report"]["bytes"], 3)
        self.assertTrue(out["report"]["truncated"])
        self.assertIsInstance(out["report"]["text"], str)

    def test_a_bad_which_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.read(which="everything")

    def test_an_unreadable_claim_marker_is_the_unreadable_path(self):
        (self.home / frr.COMPLETED_FILE).mkdir()
        out = self.read()
        self.assertEqual(out["path_taken"], frr.CLAIM_PATH_UNREADABLE)
        self.assertEqual(out["state"], frr.STATE_ABSENT)

    def test_the_report_is_read_through_sandbox_read_when_given_and_the_markers_locally(self):
        calls = []
        files = {
            f"{frr.SANDBOX_HOME}/{frr.DELIVERED_REPORT_NAME}": b"from the sandbox",
        }

        def sandbox_read(path, max_bytes):
            calls.append((path, max_bytes))
            data = files.get(path)
            return None if data is None else data[:max_bytes]

        # A local delivered file must NOT be read when the sandbox is on: the report is not here.
        self.write(frr.DELIVERED_REPORT_NAME, "local copy")
        self.write(frr.COMPLETED_FILE, frr.CLAIM_PATH_ORIGIN + "\n")
        out = self.read(max_bytes=64, sandbox_read=sandbox_read)
        self.assertEqual(out["report"]["text"], "from the sandbox")
        self.assertEqual(out["path_taken"], frr.CLAIM_PATH_ORIGIN)
        # One more byte than the cap, to tell "at the cap" from "over it".
        self.assertEqual(calls, [(f"{frr.SANDBOX_HOME}/{frr.DELIVERED_REPORT_NAME}", 65)])

    def test_the_full_report_through_the_sandbox_probes_the_ranked_files_for_the_state(self):
        files = {f"{frr.SANDBOX_HOME}/{frr.RAW_REPORT_NAME}": b"raw", f"{frr.SANDBOX_HOME}/{frr.REPORT_NAME}": b"ranked"}
        calls = []

        def sandbox_read(path, max_bytes):
            calls.append(path)
            data = files.get(path)
            return None if data is None else data[:max_bytes]

        out = self.read(which=frr.WHICH_FULL, sandbox_read=sandbox_read)
        self.assertEqual(out["state"], frr.STATE_PENDING)
        self.assertEqual(out["report"]["text"], "raw")
        self.assertIn(f"{frr.SANDBOX_HOME}/{frr.REPORT_NAME}", calls)

    def test_the_full_report_probes_nothing_when_the_markers_settle_the_state(self):
        # Retired, filed and an origin claim each decide the state on the agent pod alone; the
        # full read then spends no sandbox round-trip on the ranked files.
        for marker, text, expected in (
            (frr.REPORT_RETIRED_FILE, "1762905600.5\n", frr.STATE_RETIRED),
            (frr.REPORT_FILED_FILE, json.dumps({frr.FILED_URL: "https://x/1"}), frr.STATE_FILED),
            (frr.COMPLETED_FILE, frr.CLAIM_PATH_ORIGIN + "\n", frr.STATE_DELIVERED),
        ):
            with self.subTest(marker=marker):
                for name in (frr.REPORT_RETIRED_FILE, frr.REPORT_FILED_FILE, frr.COMPLETED_FILE):
                    (self.home / name).unlink(missing_ok=True)
                self.write(marker, text)
                calls = []

                def sandbox_read(path, max_bytes, calls=calls):
                    calls.append(path)
                    return b"raw" if path == f"{frr.SANDBOX_HOME}/{frr.RAW_REPORT_NAME}" else None

                out = self.read(which=frr.WHICH_FULL, sandbox_read=sandbox_read)
                self.assertEqual(out["state"], expected)
                self.assertEqual(out["report"]["text"], "raw")
                self.assertEqual(calls, [f"{frr.SANDBOX_HOME}/{frr.RAW_REPORT_NAME}"])

    def test_a_retired_marker_that_is_not_utf8_is_retired_with_no_time_not_a_raise(self):
        (self.home / frr.REPORT_RETIRED_FILE).write_bytes(b"\xff\xfe17629")
        out = self.read()
        self.assertEqual(out["state"], frr.STATE_RETIRED)
        self.assertIsNone(out["retired_at"])

    def test_a_sandbox_read_error_propagates_to_the_caller(self):
        def sandbox_read(path, max_bytes):
            raise RuntimeError("ssh died")

        with self.assertRaises(RuntimeError):
            self.read(sandbox_read=sandbox_read)


class OneHomeTest(unittest.TestCase):
    """The other scripts that name these files take the names from here, or are pinned equal."""

    def test_bootstrap_delivery_takes_its_names_from_here(self):
        import bootstrap_delivery

        for name in (
            "REPORT_NAME",
            "DELIVERED_REPORT_NAME",
            "COMPLETED_FILE",
            "REPORT_FILED_FILE",
            "REPORT_RETIRED_FILE",
            "FINDINGS_REGISTERED_MARKER",
            "CLAIM_PATH_ORIGIN",
            "CLAIM_PATH_CONVERSATION",
            "CLAIM_PATH_NO_HUMAN",
            "CLAIM_PATH_UNREADABLE",
            "FILED_REPO",
            "FILED_NUMBER",
            "FILED_URL",
            "FILED_AT",
            "SANDBOX_HOME",
            "CONVERSATION_DONE_FILE",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(bootstrap_delivery, name), getattr(frr, name))

    def test_the_scan_gate_takes_its_names_from_here(self):
        import bootstrap_scan_gate

        self.assertIs(bootstrap_scan_gate.COMPLETED_MARKER, frr.COMPLETED_FILE)
        self.assertEqual(bootstrap_scan_gate.INVENTORY_PATH, f"{frr.SANDBOX_HOME}/{frr.REPORT_NAME}")
        self.assertEqual(bootstrap_scan_gate.RAW_INVENTORY_PATH, f"{frr.SANDBOX_HOME}/{frr.RAW_REPORT_NAME}")

    def test_the_copies_elsewhere_are_pinned_equal(self):
        """The onboarding plugin, board_snapshot.py and findings_nudge.py keep their own copies
        (the plugin loads with no scripts directory on its path); this holds them equal."""
        import board_snapshot
        import findings_nudge

        self.assertEqual(findings_nudge.DELIVERED_MARKER, frr.COMPLETED_FILE)
        self.assertEqual(findings_nudge.CONVERSATION_DONE_MARKER, frr.CONVERSATION_DONE_FILE)
        self.assertEqual(board_snapshot.FINDINGS_REGISTERED_MARKER, frr.FINDINGS_REGISTERED_MARKER)
        plugin = (
            Path(__file__).resolve().parents[2] / "chat" / "defaults" / "plugins" / "bootstrap_onboarding" / "plugin.py"
        ).read_text(encoding="utf-8")
        self.assertIn(f'REPORT_FILED_MARKER = "{frr.REPORT_FILED_FILE}"', plugin)
        self.assertIn(f'REPORT_NAME = "{frr.REPORT_NAME}"', plugin)
        self.assertIn(f'CONVERSATION_DONE_FILE = "{frr.CONVERSATION_DONE_FILE}"', plugin)
        # Two raw literals in the plugin; a third copy, or a changed name, is caught here.
        self.assertEqual(plugin.count('".bootstrap_completed"'), 2)
        self.assertEqual(".bootstrap_completed", frr.COMPLETED_FILE)

    def test_the_raw_report_name_is_the_hand_offs_and_the_bridges(self):
        import bootstrap_handoff
        import inventory_findings

        self.assertEqual(os.path.basename(bootstrap_handoff.RAW_PATH), frr.RAW_REPORT_NAME)
        self.assertEqual(os.path.basename(inventory_findings.DEFAULT_RAW_PATH), frr.RAW_REPORT_NAME)
        self.assertEqual(os.path.dirname(bootstrap_handoff.RAW_PATH), frr.SANDBOX_HOME)


if __name__ == "__main__":
    unittest.main()
