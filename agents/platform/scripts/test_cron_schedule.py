import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import cron_schedule


class IsDailyTest(unittest.TestCase):
    def test_a_fixed_time_every_day_is_daily(self):
        for expr in ("50 6 * * *", "0 0 * * *", "  35 12 * * *  "):
            with self.subTest(expr=expr):
                self.assertTrue(cron_schedule.is_daily(expr))

    def test_anything_else_is_not(self):
        for expr in ("20 7 * * 1", "*/10 * * * *", "0 * * * *", "0 21 * * 1-5", "0 6 1 * *", "0 6,18 * * *", "", None, "@daily"):
            with self.subTest(expr=expr):
                self.assertFalse(cron_schedule.is_daily(expr))


class JobRunsDailyTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)

    def roster(self, store):
        os.makedirs(os.path.join(self.home, "cron"))
        with open(os.path.join(self.home, "cron", "jobs.json"), "w", encoding="utf-8") as handle:
            json.dump(store, handle)

    def test_reads_the_jobs_schedule(self):
        self.roster({"jobs": [
            {"id": "daily", "schedule": {"kind": "cron", "expr": "50 6 * * *"}},
            {"id": "weekly", "schedule": {"kind": "cron", "expr": "20 7 * * 1"}},
            {"id": "interval", "schedule": {"kind": "interval", "expr": "50 6 * * *"}},
        ]})
        self.assertTrue(cron_schedule.job_runs_daily(self.home, "daily"))
        self.assertFalse(cron_schedule.job_runs_daily(self.home, "weekly"))
        self.assertFalse(cron_schedule.job_runs_daily(self.home, "interval"))
        self.assertFalse(cron_schedule.job_runs_daily(self.home, "absent"))

    def test_an_unreadable_roster_is_not_daily(self):
        self.assertFalse(cron_schedule.job_runs_daily(self.home, "daily"))
        os.makedirs(os.path.join(self.home, "cron"))
        with open(os.path.join(self.home, "cron", "jobs.json"), "w", encoding="utf-8") as handle:
            handle.write("not json")
        self.assertFalse(cron_schedule.job_runs_daily(self.home, "daily"))


if __name__ == "__main__":
    unittest.main()
