import re
import unittest

from admin_console.agent_runtime import _READ_SCRIPT, TYPED_RECORDS_LIMIT, _typed_records


class ReadScriptLimitTest(unittest.TestCase):
    def test_the_in_pod_read_caps_rows_at_the_same_limit_the_portal_keeps(self) -> None:
        # The embedded script is its own program; its copy of the limit must
        # track the module's, or the pod ships rows the portal discards.
        match = re.search(r"^TYPED_RECORDS_LIMIT = (\d+)$", _READ_SCRIPT, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), TYPED_RECORDS_LIMIT)
        self.assertEqual(_READ_SCRIPT.count("ORDER BY id DESC LIMIT ?"), 3)


class TypedRecordRedactionTest(unittest.TestCase):
    def test_secret_keys_are_scrubbed_and_the_shape_survives(self) -> None:
        # The typed records bypassed the redaction every other projected field
        # gets; a worker that echoed a request header into its evidence would
        # have served the credential to every poller.
        (record,) = _typed_records(
            [
                {
                    "type": "quota_check",
                    "status": "completed",
                    "details": {
                        "request": {
                            "Authorization": "Bearer ya29.a0AfH6SMBexample",
                            "region": "us-central1",
                        },
                        "analysis": {"zones": [{"zone": "us-central1-a"}]},
                    },
                }
            ]
        )
        self.assertEqual(record["details"]["request"]["Authorization"], "[REDACTED]")
        self.assertEqual(record["details"]["request"]["region"], "us-central1")
        self.assertEqual(
            record["details"]["analysis"]["zones"], [{"zone": "us-central1-a"}]
        )
        self.assertEqual(record["type"], "quota_check")

    def test_the_newest_records_survive_the_cut(self) -> None:
        records = _typed_records([{"i": n} for n in range(TYPED_RECORDS_LIMIT + 3)])
        self.assertEqual(len(records), TYPED_RECORDS_LIMIT)
        self.assertEqual(records[-1], {"i": TYPED_RECORDS_LIMIT + 2})
        self.assertEqual(records[0], {"i": 3})

    def test_non_dict_entries_and_non_lists_are_dropped(self) -> None:
        self.assertEqual(_typed_records("not a list"), ())
        self.assertEqual(_typed_records([1, "x", {"ok": True}]), ({"ok": True},))


if __name__ == "__main__":
    unittest.main()
